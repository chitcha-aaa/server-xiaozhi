import asyncio
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
from fastapi.responses import JSONResponse
import uvicorn

from audio_converter import opus_packets_to_wav, audio_to_opus_packets, check_voice_activity
from langmind_client import LangMindClient
import speech_text as st

# Windows Thai console (cp874) can't print emoji/Thai mixed output; force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
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


# Load non-secret configuration. Credentials come from the process environment.
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
else:
    config = {}

APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
SERVER_HOST = config.get("server", {}).get("host", "0.0.0.0")
SERVER_PORT = int(os.environ.get("PORT") or os.environ.get("XIAOZHI_PORT") or config.get("server", {}).get("port", 8000))
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or config.get("server", {}).get("public_url", "")).strip()

LANGMIND_BASE_URL = os.environ.get("LANGMIND_BASE_URL", config.get("langmind", {}).get("base_url", "https://langmind.in.th/api")).strip()
LANGMIND_API_KEY = os.environ.get("LANGMIND_API_KEY", "").strip()
LANGMIND_AGENT_ID = (os.environ.get("LANGMIND_AGENT_ID") or config.get("langmind", {}).get("agent_id", "")).strip()
DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN", "").strip()
VOICE_SPEED = float(os.environ.get("VOICE_SPEED") or config.get("langmind", {}).get("voice_speed", 1.0))

SAMPLE_RATE = int(config.get("audio", {}).get("sample_rate", 16000))
FRAME_DURATION_MS = int(config.get("audio", {}).get("frame_duration_ms", 60))

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


def _runtime_ready() -> bool:
    if not (LANGMIND_API_KEY and LANGMIND_AGENT_ID) or len(DEVICE_TOKEN) < 32:
        return False
    if IS_PRODUCTION:
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
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bridge server started; runtime_ready=%s", _runtime_ready())
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

    return JSONResponse(
        content={
            "firmware": {
                "version": "1.0.0",
                "url": ""
            },
            "websocket": {
                "url": ws_url,
                "version": 1
            }
        }
    )


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    authorization = websocket.headers.get("authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if not _runtime_ready():
        await websocket.close(code=1013)
        return
    if scheme.lower() != "bearer" or not supplied_token or not hmac.compare_digest(supplied_token, DEVICE_TOKEN):
        logger.warning("Rejected unauthenticated WebSocket connection")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    session_id = str(uuid.uuid4())
    device_id = websocket.headers.get("device-id") or "authenticated-device"
    logger.info("[%s] Authenticated device connected", session_id)

    audio_buffer: list[bytes] = []
    is_listening = False
    is_speaking = False
    is_processing = False
    conversation_id = _get_conversation(device_id)
    if conversation_id:
        logger.info("[%s] Resuming device conversation", session_id)
    processing_queued = False
    connected = True  # False once the socket is gone; every pipeline checks it and stops sending

    def mark_disconnected():
        nonlocal connected
        connected = False

    turn_id = 0  # increments on every new listen/abort; stale pipelines check it and stop
    last_check_packet_count = 0
    last_audio_packet_time = asyncio.get_event_loop().time()
    watchdog_running = True

    async def vad_watchdog():
        while watchdog_running:
            await asyncio.sleep(0.25)
            if not watchdog_running:
                break
            now = asyncio.get_event_loop().time()
            if is_listening and len(audio_buffer) >= 8 and not is_processing:
                if (now - last_audio_packet_time) > 1.2:
                    logger.info(f"[{session_id}] ⏱️ Inactivity pause ({len(audio_buffer)} packets buffered). Processing speech...")
                    trigger_speech_processing()

    async def send_json(msg: dict):
        if not connected:
            return
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception:
            logger.error("[%s] Failed to send status", session_id)

    def trigger_speech_processing():
        nonlocal is_listening, processing_queued
        if is_processing or processing_queued or not audio_buffer:
            return
        is_listening = False
        processing_queued = True  # block duplicate triggers until the task starts
        asyncio.create_task(handle_user_speech())

    async def send_status(text: str, emotion: str | None = None):
        """Screen gets only allowlisted status strings — never transcript or answer text."""
        assert text in st.ALLOWED_STATUS, text
        await send_json({"session_id": session_id, "type": "stt", "text": text})
        if emotion:
            await send_json({"session_id": session_id, "type": "llm", "emotion": emotion, "text": ""})

    async def fail(status: str):
        await send_status(status, "sad")
        await send_json({"session_id": session_id, "type": "tts", "state": "stop"})

    async def tts_chunk(text: str) -> list[bytes]:
        audio = await langmind.speech(text)
        if not audio:
            return []
        return await asyncio.to_thread(audio_to_opus_packets, audio, SAMPLE_RATE, FRAME_DURATION_MS)

    async def handle_user_speech():
        nonlocal audio_buffer, is_listening, is_speaking, is_processing, conversation_id
        nonlocal last_check_packet_count, processing_queued
        processing_queued = False
        if is_processing or not audio_buffer:
            return

        is_processing = True
        is_listening = False
        my_turn = turn_id
        packets_to_process = list(audio_buffer)
        audio_buffer.clear()
        last_check_packet_count = 0

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
            wav_bytes = await asyncio.to_thread(opus_packets_to_wav, packets_to_process, SAMPLE_RATE)
            del packets_to_process
            if not wav_bytes:
                logger.warning(f"[{session_id}] Could not convert audio to WAV")
                await fail(st.STATUS_NO_SPEECH)
                return

            # 1. STT
            await send_status(st.STATUS_THINKING, "thinking")
            try:
                transcribed_text = (await langmind.transcribe(wav_bytes)).strip()
            except Exception:
                logger.error("[%s] STT request failed", session_id)
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

            t_stt = time.monotonic() - t0

            # 2+3. Chat streaming -> TTS per chunk as soon as a phrase is ready -> ordered playback.
            # (conversation_id keeps context across turns and reconnects)
            tts_tasks: asyncio.Queue = asyncio.Queue()  # ordered TTS tasks; None = end of answer
            tts_limit = asyncio.Semaphore(2)            # at most 2 TTS requests in flight
            received_answer = False
            chat_error = False
            marks: dict[str, float] = {}

            async def tts_limited(text: str) -> list[bytes]:
                async with tts_limit:
                    return await tts_chunk(text)

            async def producer():
                nonlocal conversation_id, received_answer, chat_error
                chunker = st.StreamChunker()
                try:
                    async for kind, value in langmind.chat_stream(VOICE_PREFIX + transcribed_text, conversation_id):
                        if kind == "conv":
                            if value != conversation_id:
                                conversation_id = value
                                _put_conversation(device_id, conversation_id)
                            continue
                        marks.setdefault("token", time.monotonic() - t0)
                        received_answer = True
                        for chunk in chunker.feed(value):
                            marks.setdefault("chunk", time.monotonic() - t0)
                            tts_tasks.put_nowait(asyncio.create_task(tts_limited(chunk)))
                        if stale():
                            return
                    for chunk in chunker.flush():
                        marks.setdefault("chunk", time.monotonic() - t0)
                        tts_tasks.put_nowait(asyncio.create_task(tts_limited(chunk)))
                except Exception:
                    chat_error = True
                finally:
                    tts_tasks.put_nowait(None)

            prefetch = asyncio.create_task(producer())
            is_speaking = True
            started = False
            spoken = 0
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
                    logger.info(f"[{session_id}] 🔊 First audio after {time.monotonic() - t0:.1f}s "
                                f"(STT {t_stt:.1f}s, first token {marks.get('token', 0):.1f}s, "
                                f"first chunk {marks.get('chunk', 0):.1f}s)")
                    await send_json({"session_id": session_id, "type": "tts", "state": "start"})
                    await send_status(st.STATUS_SPEAKING, "happy")
                for pkt in packets:
                    if stale():
                        break
                    try:
                        await websocket.send_bytes(pkt)
                    except Exception:
                        logger.info("[%s] Device disconnected during playback", session_id)
                        mark_disconnected()
                        break
                    await asyncio.sleep(FRAME_DURATION_MS / 1000.0)
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

    watchdog_task = asyncio.create_task(vad_watchdog())
    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            # Binary audio packet received from ESP32
            if "bytes" in msg and msg["bytes"]:
                if not is_listening or is_speaking or is_processing:
                    continue

                last_audio_packet_time = asyncio.get_event_loop().time()
                pkt = msg["bytes"]
                audio_buffer.append(pkt)

                # 1. Max speech duration -> trigger response
                if len(audio_buffer) >= MAX_SPEECH_PACKETS:
                    if not processing_queued:
                        logger.info(f"[{session_id}] ⏳ Max speech duration reached ({MAX_SPEECH_SEC:.0f}s). Processing speech...")
                    trigger_speech_processing()
                    continue

                # 2. Check for speech and trailing silence once buffer reaches at least 25 packets (~1.5s)
                # and check every 8 packets (~480ms)
                if len(audio_buffer) >= 15 and (len(audio_buffer) - last_check_packet_count) >= 4:
                    last_check_packet_count = len(audio_buffer)
                    current_packets = list(audio_buffer)
                    wav_check = await asyncio.to_thread(opus_packets_to_wav, current_packets, SAMPLE_RATE)
                    if wav_check:
                        try:
                            analysis = check_voice_activity(wav_check)
                        finally:
                            del wav_check
                        # Condition A: Speech was detected AND trailing silence detected (>600ms pause)
                        if analysis["has_speech"] and analysis["trailing_silent"]:
                            logger.info(
                                f"[{session_id}] 🤫 User finished speaking (RMS={analysis['rms']:.1f}, "
                                f"Speech={analysis['overall_speech_ratio']*100:.1f}%, TrailingSilence=True). Processing..."
                            )
                            trigger_speech_processing()
                            continue

                        # Condition B: Pure silence / no speech at all after 55 packets (~3.3 seconds)
                        elif len(audio_buffer) >= 55 and not analysis["has_speech"]:
                            logger.info(f"[{session_id}] 💤 No voice detected in 3.3s (RMS={analysis['rms']:.1f}). Clearing buffer.")
                            audio_buffer.clear()
                            last_check_packet_count = 0
                            await send_status(st.STATUS_READY, "neutral")
                            await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                            continue

            # Text JSON message received from ESP32
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    msg_type = data.get("type")

                    # Handshake hello
                    if msg_type == "hello":
                        logger.info("[%s] Device handshake completed", session_id)
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
                    elif msg_type == "listen":
                        state = data.get("state")
                        if state in ["start", "detect"]:
                            audio_buffer.clear()
                            last_check_packet_count = 0
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
        connected = False
        watchdog_running = False
        watchdog_task.cancel()
        logger.info(f"[{session_id}] Session closed")


def main():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
