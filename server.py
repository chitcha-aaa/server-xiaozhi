import asyncio
import json
import logging
import os
import socket
import sys
import time
import uuid
import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
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


# Load Configuration
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.yaml")
if not os.path.exists(CONFIG_PATH):
    logger.error(f"Configuration file not found at {CONFIG_PATH}")
    sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

SERVER_HOST = config.get("server", {}).get("host", "0.0.0.0")
SERVER_PORT = int(os.environ.get("XIAOZHI_PORT") or config.get("server", {}).get("port", 8000))
PUBLIC_URL = config.get("server", {}).get("public_url", "").strip()

LANGMIND_BASE_URL = config.get("langmind", {}).get("base_url", "https://langmind.in.th/api")
LANGMIND_API_KEY = config.get("langmind", {}).get("api_key", "").strip()
LANGMIND_AGENT_ID = config.get("langmind", {}).get("agent_id", "").strip()
VOICE_SPEED = float(config.get("langmind", {}).get("voice_speed", 1.0))

SAMPLE_RATE = int(config.get("audio", {}).get("sample_rate", 16000))
FRAME_DURATION_MS = int(config.get("audio", {}).get("frame_duration_ms", 60))

# Privacy: conversation text is not logged unless explicitly enabled for debugging.
LOG_CONVERSATION = bool(config.get("server", {}).get("log_conversation", False))
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

# Voice barge-in tuning (see websocket handler). Defaults calibrated loosely; watch "barge check" logs.
BARGE_SPEECH_RATIO = float(config.get("audio", {}).get("barge_speech_ratio", 0.5))
BARGE_RMS = float(config.get("audio", {}).get("barge_rms", 900))          # absolute floor
BARGE_FACTOR = float(config.get("audio", {}).get("barge_echo_factor", 1.8))  # x speaker-echo level
BARGE_LEARN_WINDOWS = int(config.get("audio", {}).get("barge_learn_windows", 3))
BARGE_DEBUG = bool(config.get("audio", {}).get("barge_debug", False))
_conversations: dict[str, tuple[str, float]] = {}  # device_id -> (conversation_id, last_used)
_MAX_CONVERSATIONS = 200


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
    local_ip = get_local_ip()
    ws_host = PUBLIC_URL if PUBLIC_URL else f"{local_ip}:{SERVER_PORT}"
    print("\n" + "=" * 65, flush=True)
    print("  🚀 XiaoZhi <-> LangMind Bridge Server is running!", flush=True)
    print("=" * 65, flush=True)
    print(f"  • Local IP:         {local_ip}", flush=True)
    print(f"  • Server Port:      {SERVER_PORT}", flush=True)
    print(f"  • OTA URL for ESP32: http://{ws_host}/ota/", flush=True)
    print(f"  • WebSocket URL:    ws://{ws_host}/ws", flush=True)
    print(f"  • LangMind Agent:   {LANGMIND_AGENT_ID if LANGMIND_AGENT_ID != 'YOUR_LANGMIND_AGENT_ID' else '[⚠️ NOT SET IN config.yaml]'}", flush=True)
    print("=" * 65 + "\n", flush=True)
    yield


app = FastAPI(title="XiaoZhi-LangMind Bridge", lifespan=lifespan)


@app.api_route("/ota/", methods=["GET", "POST"])
@app.api_route("/ota", methods=["GET", "POST"])
async def ota_endpoint(request: Request):
    """
    OTA endpoint called by XiaoZhi ESP32 on boot/network connection.
    Returns the WebSocket endpoint where the device should connect.
    """
    host = request.headers.get("host") or f"{get_local_ip()}:{SERVER_PORT}"
    ws_url = f"ws://{host}/ws"
    if PUBLIC_URL:
        ws_url = f"ws://{PUBLIC_URL}/ws" if not PUBLIC_URL.startswith("ws") else PUBLIC_URL

    logger.info(f"ESP32 OTA check from {request.client.host} -> telling device to connect to {ws_url}")

    # Firmware calls /ota/ only on boot (not on Wi-Fi/WebSocket reconnect), so this marks a
    # board restart: start a fresh LangMind conversation for this device.
    device_id = request.headers.get("device-id")
    if device_id and _conversations.pop(device_id, None):
        logger.info(f"Device {device_id} rebooted -> new conversation")

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
    await websocket.accept()
    client_host = websocket.client.host if websocket.client else "unknown"
    session_id = str(uuid.uuid4())
    device_id = websocket.headers.get("device-id") or client_host
    logger.info(f"[{session_id}] ESP32 connected from {client_host} (device {device_id})")

    audio_buffer: list[bytes] = []
    is_listening = False
    is_speaking = False
    is_processing = False
    total_packets_received = 0
    abort_requested = False
    conversation_id = _get_conversation(device_id)
    if conversation_id:
        logger.info(f"[{session_id}] Resuming conversation for device {device_id}")
    processing_queued = False
    listen_mode = "unknown"
    mic_during_reply = 0  # diagnostic: mic packets while thinking (STT/chat/TTS wait)
    mic_during_speaking = 0  # diagnostic: mic packets while audio is playing (needed for voice barge-in)
    barge_buffer: list[bytes] = []
    barge_echo: list[float] = []  # mic RMS while the board plays (its own speaker echo), per reply
    barge_hits = 0
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
        except Exception as e:
            logger.error(f"[{session_id}] Failed to send JSON: {e}")

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
        nonlocal audio_buffer, is_listening, is_speaking, is_processing, abort_requested, conversation_id
        nonlocal last_check_packet_count, processing_queued, mic_during_reply, mic_during_speaking
        processing_queued = False
        mic_during_reply = 0
        mic_during_speaking = 0
        barge_buffer.clear()
        barge_echo.clear()
        nonlocal barge_hits
        barge_hits = 0
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
            logger.info(f"[{session_id}] Audio buffer too small ({len(packets_to_process)} packets), ignoring")
            is_processing = False
            is_listening = True
            return

        duration_sec = len(packets_to_process) * FRAME_DURATION_MS / 1000
        logger.info(f"[{session_id}] 🎙️ Processing {len(packets_to_process)} Opus packets ({duration_sec:.1f}s)")
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
            except Exception as e:
                logger.error(f"[{session_id}] STT failed: {e}")
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
            logger.info(f"[{session_id}] STT ok ({len(transcribed_text)} chars, {time.monotonic() - t0:.1f}s)"
                        + (f": {transcribed_text!r}" if LOG_CONVERSATION else ""))

            # 2. Chat (conversation_id keeps context across turns and reconnects)
            try:
                answer_text, new_conv = await langmind.chat(VOICE_PREFIX + transcribed_text, conversation_id)
            except Exception as e:
                logger.error(f"[{session_id}] Chat failed: {e}")
                await fail(st.STATUS_ERROR_LLM)
                return
            if new_conv:
                conversation_id = new_conv
                _put_conversation(device_id, conversation_id)
            if stale():
                return
            answer_text = st.strip_greeting(answer_text or "")
            chunks = st.split_for_tts(answer_text)
            if not chunks:
                await fail(st.STATUS_ERROR_LLM)
                return
            logger.info(f"[{session_id}] Chat ok ({len(answer_text)} chars -> {len(chunks)} TTS chunks, "
                        f"{time.monotonic() - t0:.1f}s)" + (f": {answer_text!r}" if LOG_CONVERSATION else ""))

            # 3. TTS chunk by chunk; fetch the next chunk while the current one plays.
            is_speaking = True
            started = False
            prefetch = asyncio.create_task(tts_chunk(chunks[0]))
            for i in range(len(chunks)):
                try:
                    packets = await prefetch
                except Exception as e:
                    logger.error(f"[{session_id}] TTS failed on chunk {i + 1}: {e}")
                    packets = []
                prefetch = asyncio.create_task(tts_chunk(chunks[i + 1])) if i + 1 < len(chunks) else None
                if stale():
                    break
                if not packets:
                    if not started:
                        await fail(st.STATUS_ERROR_TTS)
                        return
                    continue
                if not started:
                    started = True
                    logger.info(f"[{session_id}] 🔊 First audio after {time.monotonic() - t0:.1f}s")
                    await send_json({"session_id": session_id, "type": "tts", "state": "start"})
                    await send_status(st.STATUS_SPEAKING, "happy")
                for pkt in packets:
                    if stale():
                        break
                    try:
                        await websocket.send_bytes(pkt)
                    except Exception as e:
                        logger.info(f"[{session_id}] Device gone during playback: {e}")
                        mark_disconnected()
                        break
                    await asyncio.sleep(FRAME_DURATION_MS / 1000.0)
            if stale():
                logger.info(f"[{session_id}] Playback interrupted")
            else:
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})

        except Exception as e:
            logger.error(f"[{session_id}] Error in handle_user_speech: {e}", exc_info=True)
            try:
                await fail(st.STATUS_ERROR_LLM)
            except Exception:
                pass
        finally:
            if prefetch and not prefetch.done():
                prefetch.cancel()
            logger.info(f"[{session_id}] Mic packets while thinking: {mic_during_reply}, while speaking: {mic_during_speaking} "
                        f"(listen mode={listen_mode})")
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
                if is_speaking or is_processing:
                    if is_speaking:
                        mic_during_speaking += 1
                    else:
                        mic_during_reply += 1
                    if is_speaking:
                        # Voice barge-in. The board streams mic audio while it plays, so the mic also
                        # hears its own speaker. Learn that echo level from the first windows of each
                        # reply, then interrupt only when the mic is clearly louder than the echo and
                        # VAD says it's speech, in 2 consecutive ~0.5 s windows.
                        barge_buffer.append(msg["bytes"])
                        if len(barge_buffer) >= 8:
                            wav = await asyncio.to_thread(opus_packets_to_wav, list(barge_buffer), SAMPLE_RATE)
                            barge_buffer.clear()
                            a = check_voice_activity(wav) if wav else None
                            if a:
                                rms, ratio = a["rms"], a["overall_speech_ratio"]
                                if len(barge_echo) < BARGE_LEARN_WINDOWS:
                                    barge_echo.append(rms)
                                    hit = False
                                    need = None
                                else:
                                    echo = sorted(barge_echo)[len(barge_echo) // 2]
                                    need = max(BARGE_RMS, echo * BARGE_FACTOR)
                                    hit = ratio >= BARGE_SPEECH_RATIO and rms >= need
                                barge_hits = barge_hits + 1 if hit else 0
                                if BARGE_DEBUG:
                                    logger.info(f"[{session_id}] barge check RMS={rms:.0f} Speech={ratio*100:.0f}% "
                                                f"need={'learning' if need is None else f'{need:.0f}'} hits={barge_hits}")
                                if barge_hits >= 2:
                                    logger.info(f"[{session_id}] 🗣️ Voice barge-in (RMS={rms:.0f}, Speech={ratio*100:.0f}%)")
                                    turn_id += 1
                                    is_speaking = False
                                    is_processing = False
                                    audio_buffer.clear()
                                    last_check_packet_count = 0
                                    is_listening = True
                                    # tts stop makes the board leave "speaking" and (auto mode) start listening.
                                    await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                                    await send_status(st.STATUS_LISTENING, "neutral")
                    continue

                last_audio_packet_time = asyncio.get_event_loop().time()
                pkt = msg["bytes"]
                total_packets_received += 1
                audio_buffer.append(pkt)

                # 1. Max speech duration -> trigger response
                if len(audio_buffer) >= MAX_SPEECH_PACKETS:
                    logger.info(f"[{session_id}] ⏳ Max speech duration reached ({MAX_SPEECH_SEC:.0f}s). Processing speech...")
                    trigger_speech_processing()
                    continue

                # 2. Check for speech and trailing silence once buffer reaches at least 25 packets (~1.5s)
                # and check every 8 packets (~480ms)
                if len(audio_buffer) >= 25 and (len(audio_buffer) - last_check_packet_count) >= 8:
                    last_check_packet_count = len(audio_buffer)
                    current_packets = list(audio_buffer)
                    wav_check = await asyncio.to_thread(opus_packets_to_wav, current_packets, SAMPLE_RATE)
                    if wav_check:
                        analysis = check_voice_activity(wav_check)
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
                        logger.info(f"[{session_id}] Received hello from device: {data}")
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
                        if data.get("mode"):
                            listen_mode = data["mode"]
                        logger.info(f"[{session_id}] Device listen state: {state} (mode={listen_mode})")
                        if state in ["start", "detect"]:
                            audio_buffer.clear()
                            last_check_packet_count = 0
                            if is_processing or is_speaking:
                                turn_id += 1  # barge-in: drop the old answer
                                is_processing = False
                                is_speaking = False
                            is_listening = True
                            await send_status(st.STATUS_LISTENING, "neutral")
                        elif state == "stop":
                            logger.info(f"[{session_id}] Device stopped listening (button released). Processing...")
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
    except Exception as e:
        logger.error(f"[{session_id}] WebSocket error: {e}")
    finally:
        connected = False
        watchdog_running = False
        watchdog_task.cancel()
        logger.info(f"[{session_id}] Session closed")


def main():
    local_ip = get_local_ip()
    ws_host = PUBLIC_URL if PUBLIC_URL else f"{local_ip}:{SERVER_PORT}"
    print("\n" + "=" * 65, flush=True)
    print("  XiaoZhi <--> LangMind Bridge Server is running!", flush=True)
    print("=" * 65, flush=True)
    print(f"  Local IP:         {local_ip}", flush=True)
    print(f"  Server Port:      {SERVER_PORT}", flush=True)
    print(f"  OTA URL for ESP32: http://{ws_host}/ota/", flush=True)
    print(f"  WebSocket URL:    ws://{ws_host}/ws", flush=True)
    print(f"  LangMind Agent:   {LANGMIND_AGENT_ID if LANGMIND_AGENT_ID != 'YOUR_LANGMIND_AGENT_ID' else '[NOT SET IN config.yaml]'}", flush=True)
    print("=" * 65 + "\n", flush=True)
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
