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
from typing import Optional

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


def _save_debug_wav(path: str, data: bytes) -> None:
    try:
        with open(path, "wb") as f:
            f.write(data)
    except Exception:
        pass


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
    print("  🚀 XiaoZhi <-> LangMind Bridge Server (Low-Latency Streaming)!", flush=True)
    print("=" * 65, flush=True)
    print(f"  • Local IP:         {local_ip}", flush=True)
    print(f"  • Server Port:      {SERVER_PORT}", flush=True)
    print(f"  • OTA URL for ESP32: http://{ws_host}/ota/", flush=True)
    print(f"  • WebSocket URL:    ws://{ws_host}/ws", flush=True)
    print(f"  • LangMind Agent:   {LANGMIND_AGENT_ID if LANGMIND_AGENT_ID != 'YOUR_LANGMIND_AGENT_ID' else '[⚠️ NOT SET IN config.yaml]'}", flush=True)
    print("=" * 65 + "\n", flush=True)
    try:
        yield
    finally:
        await langmind.close()


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
    vad_check_in_progress = False
    total_packets_received = 0
    abort_requested = False
    conversation_id: Optional[str] = None
    last_check_packet_count = 0
    loop = asyncio.get_running_loop()
    last_audio_packet_time = loop.time()
    watchdog_running = True

    async def vad_watchdog():
        while watchdog_running:
            await asyncio.sleep(0.12)
            if not watchdog_running:
                break
            now = loop.time()
            if is_listening and len(audio_buffer) >= 8 and not is_processing:
                # Reduced inactivity timeout from 1.2s to 0.65s for snappier turn-taking
                if (now - last_audio_packet_time) > 0.65:
                    logger.info(f"[{session_id}] ⏱️ Inactivity pause ({len(audio_buffer)} packets buffered). Processing speech...")
                    trigger_speech_processing()

    async def send_json(msg: dict):
        try:
            await websocket.send_text(json.dumps(msg))
        except Exception as e:
            logger.error(f"[{session_id}] Failed to send JSON: {e}")

    def trigger_speech_processing(
        predecoded_wav: Optional[bytes] = None,
        precomputed_analysis: Optional[dict] = None,
    ):
        nonlocal is_listening
        if is_processing or not audio_buffer:
            return
        is_listening = False
        asyncio.create_task(handle_user_speech(predecoded_wav, precomputed_analysis))

    async def run_background_vad_check(snapshot_packets: list[bytes]):
        """Runs VAD check off the WebSocket receive loop so incoming audio frames are never blocked."""
        nonlocal vad_check_in_progress
        try:
            wav_check = await asyncio.to_thread(opus_packets_to_wav, snapshot_packets, SAMPLE_RATE)
            if not wav_check or not is_listening or is_processing:
                return

            analysis = check_voice_activity(wav_check)
            # Condition A: Speech was detected AND trailing silence detected (~440ms pause)
            if analysis["has_speech"] and analysis["trailing_silent"]:
                logger.info(
                    f"[{session_id}] 🤫 User finished speaking (RMS={analysis['rms']:.1f}, "
                    f"Speech={analysis['overall_speech_ratio']*100:.1f}%, TrailingSilence=True). Processing..."
                )
                # Reuse decoded WAV if no new packets arrived during the ~35ms decode
                reuse_wav = wav_check if len(audio_buffer) == len(snapshot_packets) else None
                reuse_analysis = analysis if reuse_wav is not None else None
                trigger_speech_processing(reuse_wav, reuse_analysis)
            # Condition B: Pure silence / no speech at all after 50 packets (~3.0 seconds)
            elif len(snapshot_packets) >= 50 and not analysis["has_speech"]:
                logger.info(f"[{session_id}] 💤 No voice detected in 3.0s (RMS={analysis['rms']:.1f}). Clearing buffer.")
                audio_buffer.clear()
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
        except Exception as e:
            logger.debug(f"[{session_id}] Background VAD check error: {e}")
        finally:
            vad_check_in_progress = False

    async def handle_user_speech(
        predecoded_wav: Optional[bytes] = None,
        precomputed_analysis: Optional[dict] = None,
    ):
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

        t_start = time.perf_counter()
        duration_sec = len(packets_to_process) * (FRAME_DURATION_MS / 1000.0)
        logger.info(f"[{session_id}] 🎙️ Processing {len(packets_to_process)} Opus audio packets ({duration_sec:.1f}s)...")

        try:
            # 1. Reuse WAV from VAD check if available, otherwise convert Opus buffer to WAV in memory
            wav_bytes = predecoded_wav or await asyncio.to_thread(opus_packets_to_wav, packets_to_process, SAMPLE_RATE)
            if not wav_bytes:
                logger.warning(f"[{session_id}] Could not convert audio to WAV")
                return

            # Save last user voice asynchronously in background thread without blocking STT
            asyncio.create_task(asyncio.to_thread(_save_debug_wav, "d:/xiaozhi/last_user_voice.wav", wav_bytes))

            # 2. Quick pre-check: Does the audio contain actual human speech or just background noise?
            analysis = precomputed_analysis or check_voice_activity(wav_bytes)
            logger.info(
                f"[{session_id}] 📊 Audio analysis: RMS={analysis['rms']:.1f}, "
                f"SpeechRatio={analysis['overall_speech_ratio']*100:.1f}%, HasSpeech={analysis['has_speech']}"
            )

            # 3. STT via LangMind (update board screen to thinking state immediately)
            await send_json({"session_id": session_id, "type": "llm", "emotion": "thinking", "text": "🤔"})
            await send_json({"session_id": session_id, "type": "stt", "text": "Thinking..."})
            t_stt0 = time.perf_counter()
            transcribed_text = await langmind.transcribe(wav_bytes)
            transcribed_text = transcribed_text.strip()
            t_stt_ms = (time.perf_counter() - t_stt0) * 1000
            logger.info(f"[{session_id}] 🗣️ Transcribed text ({t_stt_ms:.0f}ms): '{transcribed_text}'")

            if not transcribed_text:
                logger.info(f"[{session_id}] No speech recognized by LangMind STT")
                await send_json({"session_id": session_id, "type": "llm", "emotion": "neutral", "text": "😐"})
                await send_json({"session_id": session_id, "type": "stt", "text": "No speech detected"})
                await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                return

            # 4. Streaming LLM + Sentence-Chunked TTS Prefetch Pipeline (No screen text required)
            abort_requested = False
            # Semaphore(1) ensures Chunk #1 gets 100% TTS priority without server contention,
            # while Chunk #2 & #3 prefetch in the background while Chunk #1 (3-4s audio) plays.
            tts_semaphore = asyncio.Semaphore(1)
            chunk_task_queue: asyncio.Queue[Optional[asyncio.Task[list[bytes]]]] = asyncio.Queue()
            pending_tasks: list[asyncio.Task] = []

            async def synthesize_chunk_to_opus(chunk_text: str, chunk_idx: int) -> list[bytes]:
                async with tts_semaphore:
                    if abort_requested:
                        return []
                    t_c0 = time.perf_counter()
                    speech_bytes = await langmind.speech(chunk_text)
                    if not speech_bytes or abort_requested:
                        return []
                    opus_pkts = await asyncio.to_thread(
                        audio_to_opus_packets, speech_bytes, SAMPLE_RATE, FRAME_DURATION_MS
                    )
                    dt_ms = (time.perf_counter() - t_c0) * 1000
                    logger.info(
                        f"[{session_id}] 🔊 TTS Chunk #{chunk_idx} ready in {dt_ms:.0f}ms: "
                        f"{len(opus_pkts)} frames ({len(opus_pkts)*0.06:.1f}s) | '{chunk_text}'"
                    )
                    return opus_pkts

            async def llm_chunk_producer():
                nonlocal conversation_id
                chunk_idx = 0
                full_answer_parts: list[str] = []
                try:
                    async for chunk_text, new_conv_id in langmind.stream_chat_chunks(transcribed_text, conversation_id):
                        if abort_requested:
                            break
                        if new_conv_id:
                            conversation_id = new_conv_id
                        chunk_idx += 1
                        full_answer_parts.append(chunk_text)
                        logger.info(
                            f"[{session_id}] 🧩 LLM Chunk #{chunk_idx} (+{(time.perf_counter()-t_start)*1000:.0f}ms): '{chunk_text}'"
                        )
                        task = asyncio.create_task(synthesize_chunk_to_opus(chunk_text, chunk_idx))
                        pending_tasks.append(task)
                        await chunk_task_queue.put(task)

                    if full_answer_parts:
                        logger.info(f"[{session_id}] 🤖 Full LangMind answer: '{' '.join(full_answer_parts)}'")
                except Exception as e:
                    logger.error(f"[{session_id}] Error in llm_chunk_producer: {e}")
                finally:
                    await chunk_task_queue.put(None)

            async def audio_stream_consumer():
                nonlocal is_speaking
                started_playback = False
                frame_duration_sec = FRAME_DURATION_MS / 1000.0
                next_frame_time = 0.0
                frames_sent = 0

                try:
                    while not abort_requested:
                        task = await chunk_task_queue.get()
                        if task is None:
                            break

                        opus_packets = await task
                        if not opus_packets or abort_requested:
                            continue

                        # Start speaker playback on the very first audio chunk
                        if not started_playback:
                            is_speaking = True
                            started_playback = True
                            ttfa_ms = (time.perf_counter() - t_start) * 1000
                            logger.info(f"[{session_id}] ⚡ Time-to-First-Audio (TTFA): {ttfa_ms:.0f}ms")
                            await send_json({"session_id": session_id, "type": "llm", "emotion": "happy", "text": "😀"})
                            await send_json({"session_id": session_id, "type": "tts", "state": "start"})
                            await send_json({"session_id": session_id, "type": "tts", "state": "sentence_start", "text": "Speaking..."})
                            next_frame_time = loop.time()

                        for pkt in opus_packets:
                            if abort_requested:
                                logger.info(f"[{session_id}] Playback aborted by user")
                                break
                            await websocket.send_bytes(pkt)
                            frames_sent += 1

                            # Burst the first 2 frames to prime ESP32 jitter buffer, then pace at exact 60ms intervals
                            if frames_sent <= 2:
                                next_frame_time = loop.time()
                            else:
                                next_frame_time += frame_duration_sec
                                delay = next_frame_time - loop.time()
                                if delay > 0:
                                    await asyncio.sleep(delay)
                                elif delay < -0.3:
                                    # Reset pacing anchor if behind by >300ms
                                    next_frame_time = loop.time()
                except Exception as e:
                    logger.warning(f"[{session_id}] Streaming audio stopped: {e}")
                finally:
                    # Cancel any remaining synthesis tasks if aborted
                    for t in pending_tasks:
                        if not t.done():
                            t.cancel()
                    if started_playback:
                        await send_json({"session_id": session_id, "type": "tts", "state": "stop"})
                    is_speaking = False

            await asyncio.gather(llm_chunk_producer(), audio_stream_consumer())

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

                last_audio_packet_time = loop.time()
                pkt = msg["bytes"]
                total_packets_received += 1
                audio_buffer.append(pkt)

                # 1. Max speech duration: 100 packets = 6.0 seconds -> trigger response!
                if len(audio_buffer) >= 100:
                    logger.info(f"[{session_id}] ⏳ Max speech duration reached (6s). Processing speech...")
                    trigger_speech_processing()
                    continue

                # 2. Non-blocking VAD check starting at 15 packets (~0.9s) and every 5 packets (~300ms)
                if (
                    len(audio_buffer) >= 15
                    and (len(audio_buffer) - last_check_packet_count) >= 5
                    and not vad_check_in_progress
                ):
                    last_check_packet_count = len(audio_buffer)
                    vad_check_in_progress = True
                    asyncio.create_task(run_background_vad_check(list(audio_buffer)))

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
                            await send_json({"session_id": session_id, "type": "llm", "emotion": "neutral", "text": "🙂"})
                            await send_json({"session_id": session_id, "type": "stt", "text": "Listening..."})
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
    print("  XiaoZhi <--> LangMind Bridge Server (Low-Latency Streaming)!", flush=True)
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
