import asyncio
import json
import logging
import os
import socket
import sys
import uuid
import yaml
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn

from audio_converter import opus_packets_to_wav, audio_to_opus_packets, check_voice_activity
from langmind_client import LangMindClient

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
SERVER_PORT = int(config.get("server", {}).get("port", 8000))
PUBLIC_URL = config.get("server", {}).get("public_url", "").strip()

LANGMIND_BASE_URL = config.get("langmind", {}).get("base_url", "https://langmind.in.th/api")
LANGMIND_API_KEY = config.get("langmind", {}).get("api_key", "").strip()
LANGMIND_AGENT_ID = config.get("langmind", {}).get("agent_id", "").strip()
VOICE_SPEED = float(config.get("langmind", {}).get("voice_speed", 1.0))

SAMPLE_RATE = int(config.get("audio", {}).get("sample_rate", 16000))
FRAME_DURATION_MS = int(config.get("audio", {}).get("frame_duration_ms", 60))

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
    logger.info(f"[{session_id}] ESP32 connected from {client_host}")

    audio_buffer: list[bytes] = []
    is_listening = False
    is_speaking = False
    is_processing = False
    total_packets_received = 0
    abort_requested = False
    conversation_id = None
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
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception as e:
            logger.error(f"[{session_id}] Failed to send JSON: {e}")

    def trigger_speech_processing():
        nonlocal is_listening
        if is_processing or not audio_buffer:
            return
        is_listening = False
        asyncio.create_task(handle_user_speech())

    async def handle_user_speech():
        nonlocal audio_buffer, is_listening, is_speaking, is_processing, abort_requested, conversation_id
        nonlocal last_check_packet_count
        if is_processing or not audio_buffer:
            return

        is_processing = True
        is_listening = False
        packets_to_process = list(audio_buffer)
        audio_buffer.clear()
        last_check_packet_count = 0

        # Need at least 8 packets (~480ms) to be meaningful audio
        if len(packets_to_process) < 8:
            logger.info(f"[{session_id}] Audio buffer too small ({len(packets_to_process)} packets), ignoring")
            is_processing = False
            is_listening = True
            return

        duration_sec = len(packets_to_process) * 0.060
        logger.info(f"[{session_id}] 🎙️ Processing {len(packets_to_process)} Opus audio packets ({duration_sec:.1f}s)...")

        try:
            # 1. Convert Opus buffer to WAV in background thread to avoid blocking event loop
            wav_bytes = await asyncio.to_thread(opus_packets_to_wav, packets_to_process, SAMPLE_RATE)
            if not wav_bytes:
                logger.warning(f"[{session_id}] Could not convert audio to WAV")
                return

            # Save last user voice for verification
            try:
                with open("d:/xiaozhi/last_user_voice.wav", "wb") as f:
                    f.write(wav_bytes)
            except Exception:
                pass

            # 2. Quick pre-check: Does the audio contain actual human speech or just background noise?
            analysis = check_voice_activity(wav_bytes)
            logger.info(
                f"[{session_id}] 📊 Audio analysis: RMS={analysis['rms']:.1f}, "
                f"SpeechRatio={analysis['overall_speech_ratio']*100:.1f}%, HasSpeech={analysis['has_speech']}"
            )

            # 3. STT via LangMind
            await send_json({"session_id": session_id, "type": "stt", "text": "กำลังฟัง..."})
            transcribed_text = await langmind.transcribe(wav_bytes)
            transcribed_text = transcribed_text.strip()
            logger.info(f"[{session_id}] 🗣️ Transcribed text: '{transcribed_text}'")

            if not transcribed_text:
                logger.info(f"[{session_id}] No speech recognized by LangMind STT")
                await send_json({"session_id": session_id, "type": "stt", "text": "ไม่ได้ยินเสียงครับ"})
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                return

            # Show transcription on ESP32 screen
            await send_json({"session_id": session_id, "type": "stt", "text": transcribed_text})

            # 4. LLM Chat via LangMind
            await send_json({"session_id": session_id, "type": "llm", "emotion": "thinking", "text": "🤔"})
            answer_text, conversation_id = await langmind.chat(transcribed_text, conversation_id)
            answer_text = answer_text.strip()
            logger.info(f"[{session_id}] 🤖 LangMind answer: '{answer_text}'")

            if not answer_text:
                answer_text = "ขออภัยครับ ไม่ได้รับคำตอบจากระบบ"

            await send_json({"session_id": session_id, "type": "llm", "emotion": "happy", "text": "😀"})

            # 5. TTS Speech via LangMind
            speech_bytes = await langmind.speech(answer_text)
            if not speech_bytes:
                logger.error(f"[{session_id}] Failed to get TTS audio from LangMind")
                return

            # 6. Convert TTS audio to Opus frames
            opus_packets = await asyncio.to_thread(audio_to_opus_packets, speech_bytes, SAMPLE_RATE, FRAME_DURATION_MS)
            logger.info(f"[{session_id}] 🔊 Generated {len(opus_packets)} Opus frames for playback")

            # 7. Stream playback to ESP32
            is_speaking = True
            abort_requested = False
            await send_json({"session_id": session_id, "type": "tts", "state": "start"})
            await send_json({"session_id": session_id, "type": "tts", "state": "sentence_start", "text": answer_text})

            try:
                for pkt in opus_packets:
                    if abort_requested:
                        logger.info(f"[{session_id}] Playback aborted by user")
                        break
                    await websocket.send_bytes(pkt)
                    await asyncio.sleep(FRAME_DURATION_MS / 1000.0)
            except Exception as e:
                logger.warning(f"[{session_id}] Streaming audio stopped: {e}")
            finally:
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                is_speaking = False

        except Exception as e:
            logger.error(f"[{session_id}] Error in handle_user_speech: {e}", exc_info=True)
        finally:
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
                    continue

                last_audio_packet_time = asyncio.get_event_loop().time()
                pkt = msg["bytes"]
                total_packets_received += 1
                audio_buffer.append(pkt)

                # 1. Max speech duration: 100 packets = 6.0 seconds -> trigger response!
                if len(audio_buffer) >= 100:
                    logger.info(f"[{session_id}] ⏳ Max speech duration reached (6s). Processing speech...")
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
                        logger.info(f"[{session_id}] Device listen state: {state}")
                        if state in ["start", "detect"]:
                            audio_buffer.clear()
                            is_listening = True
                            last_check_packet_count = 0
                            abort_requested = True  # interrupt any ongoing playback
                            await send_json({"session_id": session_id, "type": "stt", "text": "กำลังฟัง..."})
                        elif state == "stop":
                            logger.info(f"[{session_id}] Device stopped listening (button released). Processing...")
                            trigger_speech_processing()

                    # User interrupt
                    elif msg_type == "abort":
                        logger.info(f"[{session_id}] Received abort")
                        abort_requested = True
                        is_listening = False
                        audio_buffer.clear()

                except json.JSONDecodeError:
                    pass

    except WebSocketDisconnect:
        logger.info(f"[{session_id}] ESP32 disconnected")
    except Exception as e:
        logger.error(f"[{session_id}] WebSocket error: {e}")
    finally:
        watchdog_running = False
        watchdog_task.cancel()


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
