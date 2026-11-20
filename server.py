import asyncio
import datetime
import hmac
import json
import logging
import os
import socket
import sys
import time
import uuid
import yaml
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
import uvicorn

import audio_converter
from audio_converter import (opus_packets_to_pcm, opus_packets_to_wav, audio_to_opus_packets,
                            check_voice_activity, pcm_to_wav, VadTracker)
from langmind_client import LangMindClient
import speech_text as st
import screen_image

# Windows Thai console (cp874) can't print emoji/Thai mixed output; force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configure Logging
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
# LOG_FILE keeps a copy on disk. Python writes it itself: piping the server through
# Tee-Object fails on PowerShell 5.1, which wraps a native program's stderr in a
# NativeCommandError and stops the script.
_log_file = os.environ.get("LOG_FILE", "").strip()
if _log_file:
    _log_handlers.append(logging.FileHandler(_log_file, mode="a", encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
)
logger = logging.getLogger("XiaoZhiServer")


def get_local_ip() -> str:
    """Helper to detect local LAN IP address instantly without blocking."""
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _save_debug_wav(path: str, data: bytes) -> None:
    try:
        with open(path, "wb") as f:
            f.write(data)
    except Exception as exc:
        logger.debug("Failed to save debug WAV to %s: %s", path, exc)


APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# Load configuration. In production, credentials come strictly from environment variables.
# In local development, fall back to config.local.yaml if config.yaml is absent.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.local.yaml")
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
elif not IS_PRODUCTION and os.path.exists(LOCAL_CONFIG_PATH):
    with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
else:
    config = {}

SERVER_HOST = config.get("server", {}).get("host", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT") or os.environ.get("XIAOZHI_PORT") or config.get("server", {}).get("port", 8000))
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or config.get("server", {}).get("public_url", "")).strip()

LANGMIND_BASE_URL = os.environ.get("LANGMIND_BASE_URL", config.get("langmind", {}).get("base_url", "https://langmind.in.th/api")).strip()
LANGMIND_API_KEY = os.environ.get("LANGMIND_API_KEY", "").strip()
if not LANGMIND_API_KEY and not IS_PRODUCTION:
    LANGMIND_API_KEY = str(config.get("langmind", {}).get("api_key", "")).strip()
LANGMIND_AGENT_ID = (os.environ.get("LANGMIND_AGENT_ID") or config.get("langmind", {}).get("agent_id", "")).strip()
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "").strip()
# Clock handed to the board in the /ota/ reply. Defaults to Thailand, since a
# server in another region would otherwise set the board's clock to its own time.
TIMEZONE_NAME = (os.environ.get("TIMEZONE") or config.get("server", {}).get("timezone", "Asia/Bangkok")).strip()
TIMEZONE_OFFSET_MIN = float(os.environ.get("TIMEZONE_OFFSET_MIN")
                            or config.get("server", {}).get("timezone_offset_min", 420))
# Log what was heard and what was answered, for watching a conversation go by.
# Off by default: it writes what the user said into the log. The screen is a
# separate matter and never shows this text (see send_status).
LOG_TRANSCRIPT = (os.environ.get("LOG_TRANSCRIPT", "").strip().lower()
                  or str(config.get("server", {}).get("log_transcript", "")).strip().lower()) in ("1", "true", "yes", "on")
# Extra per-device tokens, so a second board can be revoked on its own without
# changing the first board's token. Format: "name:token,name:token" (env
# DEVICE_TOKENS), or a devices: {name: token} mapping in config.yaml.
def _load_device_tokens() -> dict[str, str]:
    tokens: dict[str, str] = {}
    if DEVICE_TOKEN:
        tokens["board1"] = DEVICE_TOKEN
    raw = os.environ.get("DEVICE_TOKENS", "").strip()
    for item in raw.split(","):
        name, _, value = item.partition(":")
        name, value = name.strip(), value.strip()
        if name and len(value) >= 32:
            tokens[name] = value
    for name, value in (config.get("devices") or {}).items():
        value = str(value).strip()
        if len(value) >= 32:
            tokens[str(name)] = value
    return tokens


DEVICE_TOKENS = _load_device_tokens()
# device-id (MAC) -> token, for handing each board its own token over OTA.
def _load_ota_device_tokens() -> dict[str, str]:
    pairs: dict[str, str] = {}
    for mac, name in (config.get("device_ids") or {}).items():
        pairs[str(mac).strip().lower()] = str(name)
    for item in os.environ.get("DEVICE_IDS", "").split(","):
        mac, _, name = item.rpartition(":")
        if mac.strip() and name.strip():
            pairs[mac.strip().lower()] = name.strip()
    return {mac: DEVICE_TOKENS[name] for mac, name in pairs.items() if name in DEVICE_TOKENS}


OTA_DEVICE_TOKENS = _load_ota_device_tokens()


def _match_device_token(supplied: str) -> str | None:
    """Returns the device name for a valid token, or None. Compared in constant time."""
    matched = None
    for name, value in DEVICE_TOKENS.items():
        if hmac.compare_digest(supplied, value):
            matched = name
    return matched
VOICE_SPEED = float(os.environ.get("VOICE_SPEED") or config.get("langmind", {}).get("voice_speed", 1.0))
LANGMIND_INPUT_MODE = str(config.get("langmind", {}).get("input_mode", "voice")).strip()

SAMPLE_RATE = int(config.get("audio", {}).get("sample_rate", 16000))
FRAME_DURATION_MS = int(config.get("audio", {}).get("frame_duration_ms", 60))
# The 1.4.7 board sends 3 x 20 ms SILK frames per 60 ms packet and crashes decoding single
# 60 ms SILK frames, so TTS is encoded the same way it sends.
TTS_ENCODE_FRAME_MS = int(config.get("audio", {}).get("tts_encode_frame_ms", 20))
TTS_BITRATE = str(config.get("audio", {}).get("tts_bitrate", "16k"))
TTS_COMPRESSION_LEVEL = int(config.get("audio", {}).get("tts_compression_level", 5))
# Number of concurrent TTS prefetch requests (1 gives Chunk #1 full priority; 2 overlaps more).
TTS_CONCURRENCY = max(1, int(os.environ.get("TTS_CONCURRENCY") or config.get("audio", {}).get("tts_concurrency", 2)))
# Burst the first N Opus frames immediately to prime the ESP32 jitter buffer before pacing.
TTS_BURST_FRAMES = max(0, int(config.get("audio", {}).get("tts_burst_frames", 2)))
INACTIVITY_TIMEOUT_SEC = float(config.get("audio", {}).get("inactivity_timeout_sec", 1.2))
# Optional path to save the last user utterance WAV for local debugging (disabled by default).
DEBUG_SAVE_WAV_PATH = (os.environ.get("DEBUG_SAVE_WAV_PATH")
                       or config.get("server", {}).get("debug_save_wav_path", "")).strip()
KEEP_ON_CLEAR_PACKETS = max(1, 1000 // FRAME_DURATION_MS)  # ~1 s kept when a silent window is dropped

# End-of-speech timings. Defaults are tuned for board 2 (lynai); board 1 (zuowei, quiet mic)
# may need trailing_silence_ms 800 / min_loud_speech_ms 600 back in config.yaml.
audio_converter.configure(
    trailing_silence_ms=int(config.get("audio", {}).get("trailing_silence_ms", 500)),
    min_loud_speech_ms=int(config.get("audio", {}).get("min_loud_speech_ms", 400)),
    trailing_rel_level=float(config.get("audio", {}).get("trailing_rel_level", 0.35)),
    trailing_min_rms=float(config.get("audio", {}).get("trailing_min_rms", 150)),
    aggressiveness=int(config.get("audio", {}).get("vad_aggressiveness", 3)),
)
# How often the end-of-speech check runs: every N packets once the buffer has M.
# Was 6/20 (360 ms steps, no check before 1.2 s) which alone added up to ~0.5 s before answering.
VAD_CHECK_EVERY = max(1, int(config.get("audio", {}).get("vad_check_every_packets", 3)))
VAD_CHECK_FROM = max(2, int(config.get("audio", {}).get("vad_check_min_packets", 10)))

# On-screen status wording. Allowlisted strings only (hard rule: never transcript or answer
# text). Non-ASCII wording depends on the device font - check it on the real screen.
st.configure(config.get("status_text", {}) or {})
# Which channel carries the status text to the screen: "stt" (user line), "tts"
# (assistant line, what board 2's vendor server used) or "both".
STATUS_CHANNEL = str(config.get("server", {}).get("status_channel", "both")).strip().lower()
# Draw the status as a picture and push it with the device's screen.preview_image MCP tool,
# for firmware whose UI ignores chat text (board 2's vendor build does).
SCREEN_IMAGE = str(config.get("server", {}).get("screen_image", "")).strip().lower() in ("1", "true", "yes", "on")
SCREEN_IMAGE_BASE = str(config.get("server", {}).get("screen_image_base_url", "")).strip().rstrip("/")
# Talk MCP to the device at all (needed for the screen tools and for seeing its tool list).
MCP_ENABLE = str(config.get("server", {}).get("mcp", "true")).strip().lower() in ("1", "true", "yes", "on")
# Where screen snapshots are written when /debug/snapshot is called.
SNAPSHOT_DIR = str(config.get("server", {}).get("snapshot_dir", "")).strip() or "."
if STATUS_CHANNEL not in ("stt", "tts", "both"):
    STATUS_CHANNEL = "both"

# Keep a device's LangMind conversation across Wi-Fi/WebSocket reconnects for this long (seconds).
# A board reboot that stays offline longer than this starts a fresh conversation.
CONVERSATION_TTL = int(config.get("server", {}).get("conversation_ttl_sec", 600))
# Voice reply style. Sent in front of each user utterance because the agent's own prompt
# can't be changed with this API key; keeps answers short, spoken, and without greetings.
VOICE_PREFIX = config.get("langmind", {}).get(
    "voice_prefix",
    "[คำสั่งระบบ: นี่คือการคุยด้วยเสียง ตอบคำถามทันทีเป็นภาษาพูด 1-2 ประโยคสั้นๆ "
    "ห้ามขึ้นต้นด้วยคำทักทายหรือแนะนำตัว ห้ามใช้อีโมจิหรือรายการ ถ้าคำถามไม่ชัดให้ถามกลับสั้นๆ ประโยคเดียว]\n",
)
MAX_SPEECH_SEC = float(config.get("audio", {}).get("max_speech_sec", 10))
MAX_SPEECH_PACKETS = int(MAX_SPEECH_SEC * 1000 / int(config.get("audio", {}).get("frame_duration_ms", 60)))

_conversations: dict[str, tuple[str, float]] = {}  # device_id -> (conversation_id, last_used)
_MAX_CONVERSATIONS = 200
_active_sessions: dict[str, tuple[str, object]] = {}  # device_id -> (session_id, stop_fn)


def _runtime_ready() -> bool:
    if not (LANGMIND_API_KEY and LANGMIND_AGENT_ID):
        return False
    if IS_PRODUCTION:
        if not DEVICE_TOKENS:
            return False
        parsed = _parse_websocket_url(PUBLIC_URL)
        try:
            langmind_url = urlsplit(LANGMIND_BASE_URL)
            _ = langmind_url.port
        except ValueError:
            return False
        return (
            parsed is not None
            and parsed.scheme == "wss"
            and langmind_url.scheme == "https"
            and bool(langmind_url.netloc)
            and not langmind_url.username
            and not langmind_url.password
            and not langmind_url.query
            and not langmind_url.fragment
        )
    return True


def _parse_websocket_url(value: str):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    valid = (
        parsed.scheme in {"ws", "wss"}
        and bool(parsed.netloc)
        and parsed.path == "/ws"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )
    return parsed if valid else None


def _websocket_url(request: Request) -> str:
    if PUBLIC_URL:
        parsed = _parse_websocket_url(PUBLIC_URL)
        if parsed is None or (IS_PRODUCTION and parsed.scheme != "wss"):
            raise HTTPException(status_code=503, detail="WebSocket endpoint is not configured")
        return PUBLIC_URL
    if IS_PRODUCTION:
        raise HTTPException(status_code=503, detail="WebSocket endpoint is not configured")
    host = request.headers.get("host") or f"{get_local_ip()}:{SERVER_PORT}"
    return f"ws://{host}/ws"


_snapshot_takers: dict = {}

_STATUS_SLOTS = {
    "listening": lambda: st.STATUS_LISTENING,
    "thinking": lambda: st.STATUS_THINKING,
    "speaking": lambda: st.STATUS_SPEAKING,
    "no_speech": lambda: st.STATUS_NO_SPEECH,
    "ready": lambda: st.STATUS_READY,
    "error_stt": lambda: st.STATUS_ERROR_STT,
    "error_llm": lambda: st.STATUS_ERROR_LLM,
    "error_tts": lambda: st.STATUS_ERROR_TTS,
}
_STATUS_SLOT_BY_TEXT = {}


def _status_slot(text: str) -> str | None:
    """The URL slot for an allowlisted status, rebuilt lazily so config changes are picked up."""
    if _STATUS_SLOT_BY_TEXT.get(text) is None:
        _STATUS_SLOT_BY_TEXT.clear()
        for slot, getter in _STATUS_SLOTS.items():
            _STATUS_SLOT_BY_TEXT[getter()] = slot
    return _STATUS_SLOT_BY_TEXT.get(text)


def _get_conversation(device_id: str):
    item = _conversations.get(device_id)
    if item and time.monotonic() - item[1] < CONVERSATION_TTL:
        return item[0]
    _conversations.pop(device_id, None)
    return None


def _put_conversation(device_id: str, conv_id):
    if not conv_id:
        return
    _conversations[device_id] = (conv_id, time.monotonic())
    if len(_conversations) > _MAX_CONVERSATIONS:  # bounded RAM
        oldest = min(_conversations, key=lambda k: _conversations[k][1])
        _conversations.pop(oldest, None)

langmind = LangMindClient(
    base_url=LANGMIND_BASE_URL,
    api_key=LANGMIND_API_KEY,
    agent_id=LANGMIND_AGENT_ID,
    voice_speed=VOICE_SPEED,
    input_mode=LANGMIND_INPUT_MODE,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth_mode = "token" if (DEVICE_TOKENS or IS_PRODUCTION) else "local-no-auth"
    logger.info("Bridge server started; runtime_ready=%s auth=%s", _runtime_ready(), auth_mode)
    try:
        yield
    finally:
        await langmind.aclose()


app = FastAPI(title="XiaoZhi-LangMind Bridge", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    if not _runtime_ready():
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {"status": "ready"}


@app.post("/screen/upload")
async def screen_upload(request: Request):
    """Receives a screen snapshot the device pushed (multipart, field "file", JPEG).

    This is how we see what the panel is actually drawing instead of guessing from logs.
    """
    form = await request.form()
    item = form.get("file")
    data = await item.read() if hasattr(item, "read") else (item or b"")
    if not data:
        raise HTTPException(status_code=400, detail="No image")
    name = os.path.join(SNAPSHOT_DIR, time.strftime("screen_%Y%m%d_%H%M%S.jpg"))
    with open(name, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("latin1"))
    logger.info("Screen snapshot saved: %s (%d bytes)", name, len(data))
    return {"saved": name, "bytes": len(data)}


@app.get("/debug/snapshot")
async def debug_snapshot(request: Request):
    """Asks every connected device to upload a snapshot of its screen."""
    base = SCREEN_IMAGE_BASE or f"http://{get_local_ip()}:{SERVER_PORT}"
    asked = [sid for sid, take in list(_snapshot_takers.items()) if await take(base)]
    return {"asked": asked, "base": base}


@app.get("/screen/{slot}.png")
async def screen_image_endpoint(slot: str):
    """A status string drawn as a PNG, for the device's self.screen.preview_image tool.

    Only the allowlisted statuses are reachable — the slot name selects one, so no caller
    can have arbitrary text drawn on the screen.
    """
    getter = _STATUS_SLOTS.get(slot)
    if getter is None:
        raise HTTPException(status_code=404, detail="Unknown status")
    png = await asyncio.to_thread(screen_image.render_status, getter())
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.api_route("/ota/", methods=["GET", "POST"])
@app.api_route("/ota", methods=["GET", "POST"])
async def ota_endpoint(request: Request):
    """
    OTA endpoint called by XiaoZhi ESP32 on boot/network connection.
    Returns the WebSocket endpoint where the device should connect.
    """
    if not _runtime_ready():
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    ws_url = _websocket_url(request)
    logger.info("OTA configuration requested")

    # Firmware calls /ota/ only on boot (not on Wi-Fi/WebSocket reconnect), so this marks a
    # board restart: start a fresh LangMind conversation for this device.
    device_id = request.headers.get("device-id")
    if device_id:
        _conversations.pop(device_id, None)
    # Firmware 2.1.4 saves websocket.token to NVS and sends it back as a Bearer
    # header, so a board only needs its ota_url pointed here once. Boards named in
    # OTA_DEVICE_TOKENS get their own token; everything else gets the shared one.
    ota_token = (OTA_DEVICE_TOKENS.get((device_id or "").strip().lower()) or DEVICE_TOKEN or "").strip()
    if not ota_token and DEVICE_TOKENS:
        ota_token = next(iter(DEVICE_TOKENS.values()))

    # Firmware 2.1.4 logs "No server_time section found!" without this and leaves
    # its clock unset. Field names are the ones xiaozhi 1.4.7 already parsed:
    # timestamp (ms since epoch, UTC), timezone, timezone_offset (minutes).
    now = datetime.datetime.now(datetime.timezone.utc)
    offset_min = int(round(TIMEZONE_OFFSET_MIN))
    return JSONResponse(
        content={
            "firmware": {
                "version": "1.0.0",
                "url": ""
            },
            "server_time": {
                "timestamp": int(now.timestamp() * 1000),
                "timezone": TIMEZONE_NAME,
                "timezone_offset": offset_min
            },
            "websocket": {
                "url": ws_url,
                "token": ota_token,
                "version": 1
            }
        }
    )


# Both boards land here: board 2 (SKU lynai, firmware 2.1.4) takes this URL from
# the /ota/ reply, same as board 1, and speaks the same protocol version 1 with
# the same opus 16 kHz audio. Verified with board 2 on 2026-11-18.
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    authorization = websocket.headers.get("authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if not _runtime_ready():
        await websocket.close(code=1013)
        return
    if DEVICE_TOKENS or IS_PRODUCTION:
        device_name = _match_device_token(supplied_token) if scheme.lower() == "bearer" and supplied_token else None
        if not device_name:
            logger.warning("Rejected unauthenticated WebSocket connection")
            await websocket.close(code=1008)
            return
    else:
        device_name = "local-dev"
    await websocket.accept()
    session_id = str(uuid.uuid4())
    device_id = websocket.headers.get("device-id") or "authenticated-device"
    logger.info("[%s] Authenticated device connected (%s)", session_id, device_name)

    audio_buffer: list[bytes] = []
    is_listening = False
    is_speaking = False
    is_processing = False
    conversation_id = _get_conversation(device_id)
    if conversation_id:
        logger.info("[%s] Resuming device conversation", session_id)
    processing_queued = False
    speech_confirmed = False  # set when the trailing-silence VAD already found speech in this buffer
    connected = True  # False once the socket is gone; every pipeline checks it and stops sending

    def mark_disconnected():
        nonlocal connected, is_listening, watchdog_running
        connected = False
        is_listening = False
        watchdog_running = False
        audio_buffer.clear()

    prev = _active_sessions.get(device_id)
    if prev and prev[0] != session_id:
        prev[1]()
    _active_sessions[device_id] = (session_id, mark_disconnected)

    turn_id = 0  # increments on every new listen/abort; stale pipelines check it and stop
    last_check_packet_count = 0
    vad_tracker = VadTracker(SAMPLE_RATE)  # decodes each packet once; reset with the buffer
    vad_check_in_progress = False
    vad_gen = 0  # incremented on every reset so stale background VAD results are ignored
    loop = asyncio.get_running_loop()
    last_audio_packet_time = loop.time()
    cooldown_until = 0.0
    watchdog_running = True

    def reset_vad():
        nonlocal vad_gen
        vad_gen += 1
        vad_tracker.reset()

    async def vad_watchdog():
        while watchdog_running:
            await asyncio.sleep(0.15)
            if not watchdog_running:
                break
            now = loop.time()
            if is_listening and len(audio_buffer) >= 8 and not is_processing:
                if (now - last_audio_packet_time) > INACTIVITY_TIMEOUT_SEC:
                    logger.info(f"[{session_id}] ⏱️ Inactivity pause ({len(audio_buffer)} packets buffered). Processing speech...")
                    trigger_speech_processing()

    async def run_background_vad_check(new_packets: list[bytes], snapshot_len: int, my_gen: int):
        """Runs incremental VAD decode off the WebSocket receive loop so incoming frames never stall."""
        nonlocal vad_check_in_progress, last_check_packet_count
        try:
            pcm = await asyncio.to_thread(opus_packets_to_pcm, new_packets, SAMPLE_RATE)
            if not pcm or my_gen != vad_gen or not is_listening or is_processing or processing_queued or not connected:
                return
            vad_tracker.ingest_pcm(pcm, snapshot_len)
            analysis = vad_tracker.analyze()
            logger.info(
                f"[{session_id}] VAD pkts={len(audio_buffer)} rms={analysis['rms']:.0f} "
                f"speech={analysis.get('overall_speech_ratio', 0)*100:.0f}% "
                f"trail={analysis.get('trailing_speech_ratio', 0)*100:.0f}% "
                f"loud={analysis.get('loud_speech_frames', 0)} thr={analysis.get('loud_enough', 0):.0f} "
                f"floor={analysis.get('floor', 0):.0f} peak={analysis.get('speech_level', 0):.0f} "
                f"has_speech={analysis['has_speech']} trailing_silent={analysis['trailing_silent']}"
            )
            # Condition A: Speech was detected AND trailing silence detected
            if analysis["has_speech"] and analysis["trailing_silent"]:
                logger.info(
                    f"[{session_id}] 🤫 User finished speaking (RMS={analysis['rms']:.1f}, "
                    f"Speech={analysis['overall_speech_ratio']*100:.1f}%, TrailingSilence=True). Processing..."
                )
                trigger_speech_processing(confirmed=True)
            # Condition B: Pure silence / no speech at all after 55 packets (~3.3 seconds)
            elif len(audio_buffer) >= 55 and not analysis["has_speech"]:
                logger.info(f"[{session_id}] 💤 No voice detected in 3.3s (RMS={analysis['rms']:.1f}). Clearing buffer.")
                # Keep the last ~1 s: speech that starts just before this check would
                # otherwise lose its first words (STT then returns nothing).
                del audio_buffer[:-KEEP_ON_CLEAR_PACKETS]
                last_check_packet_count = len(audio_buffer)
                reset_vad()  # frame stats belong to the dropped window
                await send_status(st.STATUS_READY, "neutral")
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
        except Exception as exc:
            logger.debug("[%s] Background VAD check error: %s", session_id, exc)
        finally:
            vad_check_in_progress = False

    async def send_json(msg: dict):
        if not connected:
            return
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception:
            logger.error("[%s] Failed to send status", session_id)

    def trigger_speech_processing(confirmed: bool = False):
        nonlocal is_listening, processing_queued, speech_confirmed
        if is_processing or processing_queued or not audio_buffer:
            return
        speech_confirmed = confirmed
        is_listening = False
        processing_queued = True  # block duplicate triggers until the task starts
        if confirmed:
            # Say "Thinking..." the moment the pause is detected. Waiting for the WAV repack
            # and the buffer re-check first left the screen on "Listening..." for a few hundred
            # ms after the user had stopped, so there was no sign the board had heard them.
            asyncio.create_task(send_status(st.STATUS_THINKING, "thinking"))
        asyncio.create_task(handle_user_speech())

    # --- MCP (device-side tools) -------------------------------------------------
    # The 2.x firmware exposes tools over JSON-RPC wrapped in {"type":"mcp","payload":...}
    # (docs/mcp-protocol.md in the firmware tree). screen.preview_image is a *user-only*
    # tool, so tools/list must be sent with withUserTools=true or it stays hidden.
    mcp_next_id = [100]
    mcp_tools: set[str] = set()
    last_screen_slot = [None]

    async def mcp_send(method: str, params: dict | None = None) -> int:
        mcp_next_id[0] += 1
        await send_json({"session_id": session_id, "type": "mcp", "payload": {
            "jsonrpc": "2.0", "method": method, "params": params or {}, "id": mcp_next_id[0]}})
        return mcp_next_id[0]

    async def take_snapshot(base: str) -> bool:
        if "self.screen.snapshot" not in mcp_tools:
            return False
        await mcp_send("tools/call", {"name": "self.screen.snapshot",
                                      "arguments": {"url": f"{base}/screen/upload", "quality": 80}})
        return True

    _snapshot_takers[session_id] = take_snapshot

    async def mcp_start():
        await mcp_send("initialize", {"capabilities": {}})
        await mcp_send("tools/list", {"cursor": "", "withUserTools": True})

    def mcp_on_payload(payload: dict):
        result = payload.get("result")
        if not isinstance(result, dict):
            if payload.get("error"):
                logger.warning("[%s] MCP error: %s", session_id, payload["error"])
            return
        tools = result.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                name = tool.get("name") if isinstance(tool, dict) else None
                if name:
                    mcp_tools.add(name)
            logger.info("[%s] MCP tools: %s", session_id, ", ".join(sorted(mcp_tools)) or "none")
            if result.get("nextCursor"):
                asyncio.create_task(mcp_send(
                    "tools/list", {"cursor": result["nextCursor"], "withUserTools": True}))

    async def show_status_image(text: str):
        """Draws the status on the screen through screen.preview_image (see screen_image.py)."""
        if not SCREEN_IMAGE or "self.screen.preview_image" not in mcp_tools:
            return
        slot = _status_slot(text)
        if slot is None or slot == last_screen_slot[0]:
            return
        last_screen_slot[0] = slot
        url = f"{SCREEN_IMAGE_BASE or f'http://{get_local_ip()}:{SERVER_PORT}'}/screen/{slot}.png"
        await mcp_send("tools/call", {"name": "self.screen.preview_image",
                                      "arguments": {"url": url}})

    async def send_status(text: str, emotion: str | None = None):
        """Screen gets only allowlisted status strings — never transcript or answer text.

        Two channels put text on the screen (main/application.cc in the 2.x firmware):
        "stt" lands on the user line (logged as ">>"), tts/sentence_start on the assistant
        line (logged as "<<"). Board 2's own vendor server used sentence_start, which is how
        it displayed Thai ("<< สวัสดี" in the 2026-11-17 capture), so that channel is the one
        proven to render on this board. STATUS_CHANNEL picks: stt, tts, or both.
        """
        assert text in st.ALLOWED_STATUS, text
        if STATUS_CHANNEL in ("stt", "both"):
            await send_json({"session_id": session_id, "type": "stt", "text": text})
        if STATUS_CHANNEL in ("tts", "both"):
            # sentence_start only sets the chat line; it does not change the device state
            # (only tts start/stop do), so it is safe to send outside playback.
            await send_json({"session_id": session_id, "type": "tts",
                             "state": "sentence_start", "text": text})
        if emotion:
            await send_json({"session_id": session_id, "type": "llm", "emotion": emotion, "text": ""})
        await show_status_image(text)

    async def fail(status: str):
        await send_status(status, "sad")
        await send_json({"session_id": session_id, "type": "tts", "state": "stop"})

    async def tts_chunk(text: str, chunk_idx: int = 0) -> list[bytes]:
        t_c0 = time.monotonic()
        audio = await langmind.speech(text)
        if not audio:
            return []
        opus_pkts = await asyncio.to_thread(
            audio_to_opus_packets, audio, SAMPLE_RATE, FRAME_DURATION_MS,
            TTS_ENCODE_FRAME_MS, TTS_BITRATE, TTS_COMPRESSION_LEVEL,
        )
        dt_ms = (time.monotonic() - t_c0) * 1000
        if chunk_idx:
            if LOG_TRANSCRIPT:
                logger.info(
                    "[%s] 🔊 TTS Chunk #%d ready in %.0fms: %d frames (%.1fs) | %s",
                    session_id, chunk_idx, dt_ms, len(opus_pkts),
                    len(opus_pkts) * (FRAME_DURATION_MS / 1000.0), text,
                )
            else:
                logger.info(
                    "[%s] 🔊 TTS Chunk #%d ready in %.0fms: %d frames (%.1fs)",
                    session_id, chunk_idx, dt_ms, len(opus_pkts),
                    len(opus_pkts) * (FRAME_DURATION_MS / 1000.0),
                )
        return opus_pkts

    async def handle_user_speech():
        nonlocal audio_buffer, is_listening, is_speaking, is_processing, conversation_id
        nonlocal last_check_packet_count, processing_queued, cooldown_until, speech_confirmed
        processing_queued = False
        already_has_speech, speech_confirmed = speech_confirmed, False
        if is_processing or not audio_buffer:
            return

        is_processing = True
        is_listening = False
        my_turn = turn_id
        packets_to_process = list(audio_buffer)
        audio_buffer.clear()
        last_check_packet_count = 0
        cached_pcm, cached_consumed, cached_analysis = vad_tracker.snapshot_pcm()
        reset_vad()

        def stale() -> bool:
            return my_turn != turn_id or not connected

        # Need at least 8 packets (~480ms) to be meaningful audio
        if len(packets_to_process) < 8:
            logger.info("[%s] Audio buffer too short", session_id)
            is_processing = False
            is_listening = True
            return

        logger.info("[%s] Processing audio", session_id)
        t0 = time.monotonic()
        prefetch: asyncio.Task | None = None

        try:
            # Reuse PCM already decoded incrementally by VadTracker to avoid a redundant FFmpeg spawn.
            wav_bytes = b""
            reused_exact_analysis = None
            if cached_pcm and cached_consumed <= len(packets_to_process):
                tail_packets = packets_to_process[cached_consumed:]
                if not tail_packets:
                    wav_bytes = pcm_to_wav(cached_pcm, SAMPLE_RATE)
                    reused_exact_analysis = cached_analysis
                else:
                    tail_pcm = await asyncio.to_thread(opus_packets_to_pcm, tail_packets, SAMPLE_RATE)
                    if tail_pcm:
                        wav_bytes = pcm_to_wav(cached_pcm + tail_pcm, SAMPLE_RATE)
            if not wav_bytes:
                wav_bytes = await asyncio.to_thread(opus_packets_to_wav, packets_to_process, SAMPLE_RATE)
            del packets_to_process, cached_pcm
            if not wav_bytes:
                logger.warning(f"[{session_id}] Could not convert audio to WAV")
                await fail(st.STATUS_NO_SPEECH)
                return

            if DEBUG_SAVE_WAV_PATH:
                asyncio.create_task(asyncio.to_thread(_save_debug_wav, DEBUG_SAVE_WAV_PATH, wav_bytes))

            analysis = reused_exact_analysis or check_voice_activity(wav_bytes)
            # The whole buffer includes the silence before and after speaking, so its speech ratio
            # is lower than the window the trailing-silence check saw. A quiet mic (RMS 400-900 on
            # the zuowei board) then fails here although speech was found, so trust that check.
            if not already_has_speech and not analysis["has_speech"]:
                del wav_bytes
                logger.info(f"[{session_id}] 💤 No speech in buffered audio (RMS={analysis['rms']:.1f}); skipping STT")
                await send_status(st.STATUS_READY, "neutral")
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                return

            # 1. STT
            await send_status(st.STATUS_THINKING, "thinking")
            try:
                transcribed_text = (await langmind.transcribe(wav_bytes)).strip()
            except Exception as e:
                logger.error("[%s] STT request failed: %s", session_id, e)
                cooldown_until = time.monotonic() + (5.0 if "429" in str(e) else 2.0)
                await fail(st.STATUS_ERROR_STT)
                return
            finally:
                del wav_bytes  # never keep user audio
            if stale():
                return
            if not transcribed_text:
                logger.info(f"[{session_id}] STT returned nothing")
                await fail(st.STATUS_NO_SPEECH)
                return
            logger.info("[%s] STT completed in %.1fs", session_id, time.monotonic() - t0)
            if LOG_TRANSCRIPT:
                logger.info("[%s] 👤 %s", session_id, transcribed_text)

            t_stt = time.monotonic() - t0

            # 2+3. Chat streaming -> TTS per chunk as soon as a phrase is ready -> ordered playback.
            # (conversation_id keeps context across turns and reconnects)
            tts_tasks: asyncio.Queue = asyncio.Queue()  # ordered TTS tasks; None = end of answer
            tts_limit = asyncio.Semaphore(TTS_CONCURRENCY)
            received_answer = False
            chat_error = False
            marks: dict[str, float] = {}

            async def tts_limited(text: str, chunk_idx: int) -> list[bytes]:
                async with tts_limit:
                    if stale():
                        return []
                    return await tts_chunk(text, chunk_idx)

            answer_parts: list[str] = []

            async def producer():
                nonlocal conversation_id, received_answer, chat_error
                chunker = st.StreamChunker()
                chunk_idx = 0
                try:
                    async for kind, value in langmind.chat_stream(VOICE_PREFIX + transcribed_text, conversation_id):
                        if kind == "conv":
                            if value != conversation_id:
                                conversation_id = value
                                _put_conversation(device_id, conversation_id)
                            continue
                        marks.setdefault("token", time.monotonic() - t0)
                        received_answer = True
                        if LOG_TRANSCRIPT:
                            answer_parts.append(value)
                        for chunk in chunker.feed(value):
                            chunk_idx += 1
                            marks.setdefault("chunk", time.monotonic() - t0)
                            if LOG_TRANSCRIPT:
                                logger.info("[%s] 🧩 LLM Chunk #%d (+%.0fms): %s",
                                            session_id, chunk_idx, (time.monotonic() - t0) * 1000, chunk)
                            tts_tasks.put_nowait(asyncio.create_task(tts_limited(chunk, chunk_idx)))
                        if stale():
                            return
                    for chunk in chunker.flush():
                        chunk_idx += 1
                        marks.setdefault("chunk", time.monotonic() - t0)
                        if LOG_TRANSCRIPT:
                            logger.info("[%s] 🧩 LLM Chunk #%d (+%.0fms): %s",
                                        session_id, chunk_idx, (time.monotonic() - t0) * 1000, chunk)
                        tts_tasks.put_nowait(asyncio.create_task(tts_limited(chunk, chunk_idx)))
                except Exception:
                    chat_error = True
                finally:
                    # Log the answer as soon as the LLM finished it, not after playback ends,
                    # so the console shows the exchange while the board is still speaking.
                    if LOG_TRANSCRIPT and answer_parts:
                        logger.info("[%s] 🤖 %s", session_id, "".join(answer_parts).strip())
                    tts_tasks.put_nowait(None)

            prefetch = asyncio.create_task(producer())
            is_speaking = True
            started = False
            spoken = 0
            frames_sent = 0
            frame_duration_sec = FRAME_DURATION_MS / 1000.0
            next_frame_time = 0.0
            while True:
                task = await tts_tasks.get()
                if task is None:
                    break
                try:
                    packets = await task
                except Exception:
                    logger.error("[%s] TTS request failed", session_id)
                    packets = []
                spoken += 1
                if stale():
                    task.cancel()
                    break
                if not packets:
                    if not started:
                        await fail(st.STATUS_ERROR_TTS)
                        return
                    continue
                if not started:
                    started = True
                    ttfa_sec = time.monotonic() - t0
                    logger.info(f"[{session_id}] 🔊 First audio after {ttfa_sec:.1f}s (TTFA {ttfa_sec*1000:.0f}ms; "
                                f"STT {t_stt:.1f}s, first token {marks.get('token', 0):.1f}s, "
                                f"first chunk {marks.get('chunk', 0):.1f}s)")
                    await send_json({"session_id": session_id, "type": "tts", "state": "start"})
                    await send_status(st.STATUS_SPEAKING, "happy")
                    next_frame_time = loop.time()
                for pkt in packets:
                    if stale():
                        break
                    try:
                        await websocket.send_bytes(pkt)
                    except Exception:
                        logger.info("[%s] Device disconnected during playback", session_id)
                        mark_disconnected()
                        break
                    frames_sent += 1
                    # Burst the first N frames to prime the ESP32 jitter buffer, then pace drift-free.
                    if frames_sent <= TTS_BURST_FRAMES:
                        next_frame_time = loop.time()
                    else:
                        next_frame_time += frame_duration_sec
                        delay = next_frame_time - loop.time()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        elif delay < -0.3:
                            next_frame_time = loop.time()
            # drop any TTS still queued after an interrupt
            while not tts_tasks.empty():
                t = tts_tasks.get_nowait()
                if t is not None:
                    t.cancel()
            has_answer = received_answer
            if has_answer or chat_error:
                logger.info("[%s] Chat completed; audio chunks=%s", session_id, spoken)
            if chat_error:
                logger.error("[%s] Chat request failed", session_id)
            if stale():
                logger.info(f"[{session_id}] Playback interrupted")
            elif not started:
                await fail(st.STATUS_ERROR_LLM if (chat_error or not has_answer) else st.STATUS_ERROR_TTS)
            else:
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})

        except Exception:
            logger.error("[%s] Speech pipeline failed", session_id)
            try:
                await fail(st.STATUS_ERROR_LLM)
            except Exception:
                pass
        finally:
            if prefetch and not prefetch.done():
                prefetch.cancel()
            if not stale():  # a newer turn owns the state after barge-in
                is_speaking = False
                is_processing = False
                is_listening = True
                audio_buffer.clear()
                last_check_packet_count = 0
                reset_vad()

    watchdog_task = asyncio.create_task(vad_watchdog())
    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            # Binary audio packet received from ESP32
            if "bytes" in msg and msg["bytes"]:
                if not is_listening or is_speaking or is_processing or time.monotonic() < cooldown_until:
                    continue

                last_audio_packet_time = loop.time()
                pkt = msg["bytes"]
                audio_buffer.append(pkt)

                # 1. Max speech duration -> trigger response
                if len(audio_buffer) >= MAX_SPEECH_PACKETS:
                    if not processing_queued:
                        logger.info(f"[{session_id}] ⏳ Max speech duration reached ({MAX_SPEECH_SEC:.0f}s). Processing speech...")
                    trigger_speech_processing()
                    continue

                # 2. Non-blocking incremental VAD check once the buffer reaches VAD_CHECK_FROM
                # packets, then every VAD_CHECK_EVERY packets. Only the new packets are decoded.
                if (
                    len(audio_buffer) >= VAD_CHECK_FROM
                    and (len(audio_buffer) - last_check_packet_count) >= VAD_CHECK_EVERY
                    and not vad_check_in_progress
                ):
                    snapshot_len = len(audio_buffer)
                    new_packets = audio_buffer[vad_tracker.consumed:snapshot_len]
                    if new_packets:
                        last_check_packet_count = snapshot_len
                        vad_check_in_progress = True
                        asyncio.create_task(run_background_vad_check(new_packets, snapshot_len, vad_gen))

            # Text JSON message received from ESP32
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    msg_type = data.get("type")

                    # Handshake hello
                    if msg_type == "hello":
                        logger.info("[%s] Device handshake completed", session_id)
                        if MCP_ENABLE:
                            asyncio.create_task(mcp_start())
                        await send_json({
                            "type": "hello",
                            "transport": "websocket",
                            "session_id": session_id,
                            "audio_params": {
                                "format": "opus",
                                "sample_rate": SAMPLE_RATE,
                                "channels": 1,
                                "frame_duration": FRAME_DURATION_MS,
                            },
                        })

                    # Listen state change
                    elif msg_type == "mcp":
                        payload = data.get("payload")
                        if isinstance(payload, dict):
                            mcp_on_payload(payload)

                    elif msg_type == "listen":
                        state = data.get("state")
                        if state in ["start", "detect"]:
                            cooldown_until = 0.0
                            audio_buffer.clear()
                            last_check_packet_count = 0
                            reset_vad()
                            if is_processing or is_speaking:
                                turn_id += 1  # a new button-initiated listen turn supersedes old output
                                is_processing = False
                                is_speaking = False
                            is_listening = True
                            await send_status(st.STATUS_LISTENING, "neutral")
                        elif state == "stop":
                            logger.info("[%s] Button stopped listening", session_id)
                            trigger_speech_processing()

                    # User interrupt
                    elif msg_type == "abort":
                        logger.info(f"[{session_id}] Received abort")
                        turn_id += 1
                        is_listening = False
                        is_processing = False
                        is_speaking = False
                        audio_buffer.clear()
                        last_check_packet_count = 0
                        reset_vad()
                        # The board stays in "speaking" until it gets tts stop; without this every
                        # later button press is just another abort and it never listens again.
                        await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                        await send_status(st.STATUS_READY, "neutral")

                except json.JSONDecodeError:
                    pass

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] ESP32 disconnected")
    except Exception:
        logger.error("[%s] WebSocket session failed", session_id)
    finally:
        mark_disconnected()
        if _active_sessions.get(device_id, (None,))[0] == session_id:
            _active_sessions.pop(device_id, None)
        _snapshot_takers.pop(session_id, None)
        watchdog_task.cancel()
        logger.info(f"[{session_id}] Session closed")


def main():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
