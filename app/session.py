"""Per-board WebSocket session and multi-device session/conversation manager."""
import asyncio
import json
import logging
import time
import uuid
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect

from app.audio_converter import (
    VadTracker,
    audio_to_opus_packets,
    check_voice_activity,
    opus_packets_to_wav,
)
from app.devices import DeviceContext
from app import speech_text as st

logger = logging.getLogger("XiaoZhiSession")


class SessionManager:
    """Tracks active board sessions, LangMind conversation IDs, and snapshot hooks."""

    def __init__(self, conversation_ttl: int = 600, max_conversations: int = 200):
        self.conversation_ttl = conversation_ttl
        self.max_conversations = max_conversations
        self._conversations: dict[str, tuple[str, float]] = {}
        # device_id -> (session_id, disconnect_fn, profile_name)
        self._active_sessions: dict[str, tuple[str, Callable[[], None], str]] = {}
        self._snapshot_takers: dict[str, Callable] = {}

    def clear_conversation(self, device_id: str | None) -> None:
        if device_id:
            self._conversations.pop(device_id, None)

    def get_conversation(self, device_id: str) -> str | None:
        item = self._conversations.get(device_id)
        if item and time.monotonic() - item[1] < self.conversation_ttl:
            return item[0]
        self._conversations.pop(device_id, None)
        return None

    def put_conversation(self, device_id: str, conv_id: str | None) -> None:
        if not conv_id:
            return
        self._conversations[device_id] = (conv_id, time.monotonic())
        if len(self._conversations) > self.max_conversations:
            oldest = min(self._conversations, key=lambda k: self._conversations[k][1])
            self._conversations.pop(oldest, None)

    def register_session(
        self,
        device_id: str,
        session_id: str,
        stop_fn: Callable[[], None],
        profile_name: str = "default",
    ) -> None:
        prev = self._active_sessions.get(device_id)
        if prev and prev[0] != session_id:
            prev[1]()
        self._active_sessions[device_id] = (session_id, stop_fn, profile_name)

    def unregister_session(self, device_id: str, session_id: str) -> None:
        cur = self._active_sessions.get(device_id)
        if cur and cur[0] == session_id:
            self._active_sessions.pop(device_id, None)

    def disconnect_device(self, device_id: str) -> bool:
        """Immediately closes the active WebSocket session for a board (e.g. on revoke or profile change)."""
        cur = self._active_sessions.get(device_id)
        if not cur:
            return False
        cur[1]()
        return True

    def disconnect_profile(self, profile_name: str) -> list[str]:
        """Immediately closes all active WebSocket sessions using the given profile."""
        affected = [
            dev_id for dev_id, (_, _, pname) in list(self._active_sessions.items())
            if pname == profile_name
        ]
        for dev_id in affected:
            self.disconnect_device(dev_id)
        return affected

    def active_device_ids(self) -> list[str]:
        return list(self._active_sessions.keys())

    def register_snapshot_taker(self, session_id: str, taker: Callable) -> None:
        self._snapshot_takers[session_id] = taker

    def unregister_snapshot_taker(self, session_id: str) -> None:
        self._snapshot_takers.pop(session_id, None)

    async def take_all_snapshots(self, base_url: str) -> list[str]:
        asked = []
        for sid, take in list(self._snapshot_takers.items()):
            if await take(base_url):
                asked.append(sid)
        return asked


class DeviceSession:
    """Handles one connected ESP32 board's full WebSocket lifecycle."""

    def __init__(
        self,
        websocket: WebSocket,
        device: DeviceContext,
        langmind,
        sessions: SessionManager,
        ffmpeg_sem: asyncio.Semaphore,
        *,
        voice_prefix: str = "",
        screen_image_base: str = "",
        log_transcript: bool = False,
        local_ip_fn: Callable[[], str] = lambda: "127.0.0.1",
        server_port: int = 8000,
    ):
        self.websocket = websocket
        self.device = device
        self.profile = device.profile
        self.audio = device.profile.audio
        self.vad = device.profile.vad
        self.screen = device.profile.screen
        self.status = device.profile.status

        self.langmind = langmind
        self.sessions = sessions
        self.ffmpeg_sem = ffmpeg_sem
        self.voice_prefix = voice_prefix
        self.screen_image_base = screen_image_base
        self.log_transcript = log_transcript
        self.local_ip_fn = local_ip_fn
        self.server_port = server_port

        self.session_id = str(uuid.uuid4())
        self.tag = f"[{self.session_id[:8]}:{self.device.name}]"

        self.audio_buffer: list[bytes] = []
        self.is_listening = False
        self.is_speaking = False
        self.is_processing = False
        self.processing_queued = False
        self.speech_confirmed = False
        self.connected = True
        self.watchdog_running = True

        self.conversation_id = self.sessions.get_conversation(self.device.device_id)
        self.turn_id = 0
        self.last_check_packet_count = 0
        self.vad_tracker = VadTracker(self.audio.sample_rate, self.vad)
        self.last_audio_packet_time = 0.0
        self.cooldown_until = 0.0

        self.mcp_next_id = 100
        self.mcp_tools: set[str] = set()
        self.last_screen_slot: str | None = None

    def mark_disconnected(self) -> None:
        self.connected = False
        self.is_listening = False
        self.watchdog_running = False
        self.audio_buffer.clear()

    def force_disconnect(self) -> None:
        """Marks session disconnected and actively closes the WebSocket."""
        was_connected = self.connected
        self.mark_disconnected()
        if was_connected:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._close_ws_quietly())
            except RuntimeError:
                pass

    async def _close_ws_quietly(self) -> None:
        try:
            await self.websocket.close(code=1008)
        except Exception:
            pass

    async def send_json(self, msg: dict) -> None:
        if not self.connected:
            return
        try:
            await self.websocket.send_text(json.dumps(msg))
        except Exception:
            logger.error("%s Failed to send status", self.tag)

    # --- MCP (device-side tools) -------------------------------------------------

    async def mcp_send(self, method: str, params: dict | None = None) -> int:
        self.mcp_next_id += 1
        await self.send_json({
            "session_id": self.session_id,
            "type": "mcp",
            "payload": {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": self.mcp_next_id,
            },
        })
        return self.mcp_next_id

    async def mcp_start(self) -> None:
        await self.mcp_send("initialize", {"capabilities": {}})
        await self.mcp_send("tools/list", {"cursor": "", "withUserTools": True})

    def mcp_on_payload(self, payload: dict) -> None:
        result = payload.get("result")
        if not isinstance(result, dict):
            if payload.get("error"):
                logger.warning("%s MCP error: %s", self.tag, payload["error"])
            return
        tools = result.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                name = tool.get("name") if isinstance(tool, dict) else None
                if name:
                    self.mcp_tools.add(name)
            logger.info("%s MCP tools: %s", self.tag, ", ".join(sorted(self.mcp_tools)) or "none")
            if result.get("nextCursor"):
                asyncio.create_task(self.mcp_send(
                    "tools/list", {"cursor": result["nextCursor"], "withUserTools": True}
                ))

    async def take_snapshot(self, base: str) -> bool:
        if "self.screen.snapshot" not in self.mcp_tools:
            return False
        await self.mcp_send("tools/call", {
            "name": "self.screen.snapshot",
            "arguments": {
                "url": f"{base}/screen/upload?device={self.device.name}",
                "quality": 80,
            },
        })
        return True

    def _http_base_url(self) -> str:
        if self.screen_image_base:
            return self.screen_image_base
        host = (self.websocket.headers.get("host") or "").strip()
        if host:
            scheme = "https" if self.websocket.url.scheme == "wss" else "http"
            # Some ESP32 WebSocket clients send Host without the port (e.g. "192.168.1.5"
            # instead of "192.168.1.5:8000"), which would make preview_image hit port 80
            # and block the board for ~18s waiting for a TCP timeout.
            if ":" not in host and (
                (scheme == "http" and self.server_port != 80)
                or (scheme == "https" and self.server_port != 443)
            ):
                host = f"{host}:{self.server_port}"
            return f"{scheme}://{host}"
        return f"http://{self.local_ip_fn()}:{self.server_port}"

    async def show_status_image(self, text: str) -> None:
        """Draws the status on the screen through screen.preview_image (see screen_image.py)."""
        if not self.screen.screen_image or "self.screen.preview_image" not in self.mcp_tools:
            return
        slot = self.status.slot_for_text(text)
        if slot is None or slot == self.last_screen_slot:
            return
        self.last_screen_slot = slot
        url = f"{self._http_base_url()}/screen/{self.profile.name}/{slot}.png"
        await self.mcp_send("tools/call", {
            "name": "self.screen.preview_image",
            "arguments": {"url": url},
        })

    async def send_status(self, text: str, emotion: str | None = None) -> None:
        """Screen gets only allowlisted status strings — never transcript or answer text."""
        assert text in self.status.allowed, text
        if self.screen.status_channel in ("stt", "both"):
            await self.send_json({"session_id": self.session_id, "type": "stt", "text": text})
        if self.screen.status_channel in ("tts", "both"):
            await self.send_json({
                "session_id": self.session_id,
                "type": "tts",
                "state": "sentence_start",
                "text": text,
            })
        if emotion:
            await self.send_json({
                "session_id": self.session_id,
                "type": "llm",
                "emotion": emotion,
                "text": "",
            })
        await self.show_status_image(text)

    async def fail(self, status_text: str) -> None:
        await self.send_status(status_text, "sad")
        await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})

    # --- Audio & Speech Pipeline -------------------------------------------------

    async def _tts_chunk(self, text: str) -> list[bytes]:
        audio = await self.langmind.speech(text)
        if not audio:
            return []
        async with self.ffmpeg_sem:
            return await asyncio.to_thread(
                audio_to_opus_packets,
                audio,
                self.audio.sample_rate,
                self.audio.frame_duration_ms,
                self.audio.tts_encode_frame_ms,
                self.audio.tts_bitrate,
            )

    def trigger_speech_processing(self, confirmed: bool = False) -> None:
        if self.is_processing or self.processing_queued or not self.audio_buffer:
            return
        self.speech_confirmed = confirmed
        self.is_listening = False
        self.processing_queued = True
        if confirmed:
            asyncio.create_task(self.send_status(self.status.thinking, "thinking"))
        asyncio.create_task(self.handle_user_speech())

    async def vad_watchdog(self) -> None:
        while self.watchdog_running:
            await asyncio.sleep(0.25)
            if not self.watchdog_running:
                break
            now = asyncio.get_running_loop().time()
            if self.is_listening and len(self.audio_buffer) >= 8 and not self.is_processing:
                if (now - self.last_audio_packet_time) > 1.2:
                    logger.info(
                        "%s Inactivity pause (%d packets buffered). Processing speech...",
                        self.tag, len(self.audio_buffer),
                    )
                    self.trigger_speech_processing()

    async def handle_user_speech(self) -> None:
        self.processing_queued = False
        already_has_speech, self.speech_confirmed = self.speech_confirmed, False
        if self.is_processing or not self.audio_buffer:
            return

        self.is_processing = True
        self.is_listening = False
        my_turn = self.turn_id
        packets_to_process = list(self.audio_buffer)
        self.audio_buffer.clear()
        self.last_check_packet_count = 0
        self.vad_tracker.reset()

        def stale() -> bool:
            return my_turn != self.turn_id or not self.connected

        if len(packets_to_process) < 8:
            logger.info("%s Audio buffer too short", self.tag)
            self.is_processing = False
            self.is_listening = True
            return

        logger.info("%s Processing audio", self.tag)
        t0 = time.monotonic()
        prefetch: asyncio.Task | None = None

        try:
            async with self.ffmpeg_sem:
                wav_bytes = await asyncio.to_thread(
                    opus_packets_to_wav, packets_to_process, self.audio.sample_rate
                )
            del packets_to_process
            if not wav_bytes:
                logger.warning("%s Could not convert audio to WAV", self.tag)
                await self.fail(self.status.no_speech)
                return

            analysis = check_voice_activity(wav_bytes, self.vad, self.audio.sample_rate)
            if not already_has_speech and not analysis["has_speech"]:
                del wav_bytes
                logger.info(
                    "%s No speech in buffered audio (RMS=%.1f); skipping STT",
                    self.tag, analysis["rms"],
                )
                await self.send_status(self.status.ready, "neutral")
                await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
                return

            # 1. STT
            await self.send_status(self.status.thinking, "thinking")
            try:
                transcribed_text = (await self.langmind.transcribe(wav_bytes)).strip()
            except Exception as e:
                logger.error("%s STT request failed: %s", self.tag, e)
                self.cooldown_until = time.monotonic() + (5.0 if "429" in str(e) else 2.0)
                await self.fail(self.status.error_stt)
                return
            finally:
                del wav_bytes  # never keep user audio
            if stale():
                return
            if not transcribed_text:
                logger.info("%s STT returned nothing", self.tag)
                await self.fail(self.status.no_speech)
                return
            logger.info("%s STT completed in %.1fs", self.tag, time.monotonic() - t0)
            if self.log_transcript:
                logger.info("%s User: %s", self.tag, transcribed_text)

            t_stt = time.monotonic() - t0

            # 2+3. Chat streaming -> TTS per chunk -> ordered playback
            tts_tasks: asyncio.Queue = asyncio.Queue()
            tts_limit = asyncio.Semaphore(2)
            received_answer = False
            chat_error = False
            marks: dict[str, float] = {}

            async def tts_limited(text: str) -> list[bytes]:
                async with tts_limit:
                    return await self._tts_chunk(text)

            answer_parts: list[str] = []

            async def producer():
                nonlocal received_answer, chat_error
                chunker = st.StreamChunker()
                try:
                    async for kind, value in self.langmind.chat_stream(
                        self.voice_prefix + transcribed_text, self.conversation_id
                    ):
                        if kind == "conv":
                            if value != self.conversation_id:
                                self.conversation_id = value
                                self.sessions.put_conversation(self.device.device_id, self.conversation_id)
                            continue
                        marks.setdefault("token", time.monotonic() - t0)
                        received_answer = True
                        if self.log_transcript:
                            answer_parts.append(value)
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
                    if self.log_transcript and answer_parts:
                        logger.info("%s Assistant: %s", self.tag, "".join(answer_parts).strip())
                    tts_tasks.put_nowait(None)

            prefetch = asyncio.create_task(producer())
            self.is_speaking = True
            started = False
            spoken = 0
            while True:
                task = await tts_tasks.get()
                if task is None:
                    break
                try:
                    packets = await task
                except Exception:
                    logger.error("%s TTS request failed", self.tag)
                    packets = []
                spoken += 1
                if stale():
                    task.cancel()
                    break
                if not packets:
                    if not started:
                        await self.fail(self.status.error_tts)
                        return
                    continue
                if not started:
                    started = True
                    logger.info(
                        "%s First audio after %.1fs (STT %.1fs, first token %.1fs, first chunk %.1fs)",
                        self.tag,
                        time.monotonic() - t0,
                        t_stt,
                        marks.get("token", 0.0),
                        marks.get("chunk", 0.0),
                    )
                    await self.send_json({"session_id": self.session_id, "type": "tts", "state": "start"})
                    await self.send_status(self.status.speaking, "happy")
                for pkt in packets:
                    if stale():
                        break
                    try:
                        await self.websocket.send_bytes(pkt)
                    except Exception:
                        logger.info("%s Device disconnected during playback", self.tag)
                        self.mark_disconnected()
                        break
                    await asyncio.sleep(self.audio.frame_duration_ms / 1000.0)

            while not tts_tasks.empty():
                t = tts_tasks.get_nowait()
                if t is not None:
                    t.cancel()

            has_answer = received_answer
            if has_answer or chat_error:
                logger.info("%s Chat completed; audio chunks=%s", self.tag, spoken)
            if chat_error:
                logger.error("%s Chat request failed", self.tag)
            if stale():
                logger.info("%s Playback interrupted", self.tag)
            elif not started:
                await self.fail(self.status.error_llm if (chat_error or not has_answer) else self.status.error_tts)
            else:
                await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})

        except Exception:
            logger.error("%s Speech pipeline failed", self.tag)
            try:
                await self.fail(self.status.error_llm)
            except Exception:
                pass
        finally:
            if prefetch and not prefetch.done():
                prefetch.cancel()
            if not stale():
                self.is_speaking = False
                self.is_processing = False
                self.is_listening = True
                self.audio_buffer.clear()
                self.last_check_packet_count = 0
                self.vad_tracker.reset()

    # --- Main WebSocket Loop -----------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "%s Authenticated device connected (profile=%s, device_id=%s)",
            self.tag, self.profile.name, self.device.device_id,
        )
        if self.conversation_id:
            logger.info("%s Resuming device conversation", self.tag)

        self.sessions.register_session(
            self.device.device_id,
            self.session_id,
            self.force_disconnect,
            profile_name=self.profile.name,
        )
        self.sessions.register_snapshot_taker(self.session_id, self.take_snapshot)
        self.last_audio_packet_time = asyncio.get_running_loop().time()
        watchdog_task = asyncio.create_task(self.vad_watchdog())

        try:
            while True:
                msg = await self.websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break

                if "bytes" in msg and msg["bytes"]:
                    await self._handle_audio_frame(msg["bytes"])
                elif "text" in msg and msg["text"]:
                    await self._handle_text_message(msg["text"])
        except WebSocketDisconnect:
            logger.info("%s ESP32 disconnected", self.tag)
        except Exception:
            logger.error("%s WebSocket session failed", self.tag)
        finally:
            self.mark_disconnected()
            self.sessions.unregister_session(self.device.device_id, self.session_id)
            self.sessions.unregister_snapshot_taker(self.session_id)
            watchdog_task.cancel()
            logger.info("%s Session closed", self.tag)

    async def _handle_audio_frame(self, pkt: bytes) -> None:
        if not self.is_listening or self.is_speaking or self.is_processing or time.monotonic() < self.cooldown_until:
            return

        self.last_audio_packet_time = asyncio.get_running_loop().time()
        self.audio_buffer.append(pkt)

        # 1. Max speech duration -> trigger response
        if len(self.audio_buffer) >= self.audio.max_speech_packets:
            if not self.processing_queued:
                logger.info(
                    "%s Max speech duration reached (%.0fs). Processing speech...",
                    self.tag, self.audio.max_speech_sec,
                )
            self.trigger_speech_processing()
            return

        # 2. Incremental VAD check
        if (
            len(self.audio_buffer) >= self.audio.vad_check_min_packets
            and (len(self.audio_buffer) - self.last_check_packet_count) >= self.audio.vad_check_every_packets
        ):
            self.last_check_packet_count = len(self.audio_buffer)
            current_packets = list(self.audio_buffer)
            async with self.ffmpeg_sem:
                ok = await asyncio.to_thread(self.vad_tracker.feed, current_packets)
            if ok:
                analysis = self.vad_tracker.analyze()
                logger.info(
                    "%s VAD pkts=%d rms=%.0f speech=%.0f%% trail=%.0f%% loud=%d thr=%.0f "
                    "floor=%.0f peak=%.0f has_speech=%s trailing_silent=%s",
                    self.tag,
                    len(self.audio_buffer),
                    analysis["rms"],
                    analysis.get("overall_speech_ratio", 0) * 100,
                    analysis.get("trailing_speech_ratio", 0) * 100,
                    analysis.get("loud_speech_frames", 0),
                    analysis.get("loud_enough", 0),
                    analysis.get("floor", 0),
                    analysis.get("speech_level", 0),
                    analysis["has_speech"],
                    analysis["trailing_silent"],
                )
                if analysis["has_speech"] and analysis["trailing_silent"]:
                    logger.info(
                        "%s User finished speaking (RMS=%.1f, Speech=%.1f%%). Processing...",
                        self.tag, analysis["rms"], analysis["overall_speech_ratio"] * 100,
                    )
                    self.trigger_speech_processing(confirmed=True)
                elif len(self.audio_buffer) >= 55 and not analysis["has_speech"]:
                    logger.info(
                        "%s No voice detected in 3.3s (RMS=%.1f). Clearing buffer.",
                        self.tag, analysis["rms"],
                    )
                    del self.audio_buffer[:-self.audio.keep_on_clear_packets]
                    self.last_check_packet_count = len(self.audio_buffer)
                    self.vad_tracker.reset()
                    await self.send_status(self.status.ready, "neutral")
                    await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})

    async def _handle_text_message(self, raw_text: str) -> None:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return
        msg_type = data.get("type")

        if msg_type == "hello":
            logger.info("%s Device handshake completed", self.tag)
            if self.screen.mcp_enabled:
                asyncio.create_task(self.mcp_start())
            await self.send_json({
                "type": "hello",
                "transport": "websocket",
                "session_id": self.session_id,
                "audio_params": {
                    "format": "opus",
                    "sample_rate": self.audio.sample_rate,
                    "channels": 1,
                    "frame_duration": self.audio.frame_duration_ms,
                },
            })

        elif msg_type == "mcp":
            payload = data.get("payload")
            if isinstance(payload, dict):
                self.mcp_on_payload(payload)

        elif msg_type == "listen":
            state = data.get("state")
            if state in ("start", "detect"):
                self.cooldown_until = 0.0
                self.audio_buffer.clear()
                self.last_check_packet_count = 0
                self.vad_tracker.reset()
                if self.is_processing or self.is_speaking:
                    self.turn_id += 1
                    self.is_processing = False
                    self.is_speaking = False
                self.is_listening = True
                await self.send_status(self.status.listening, "neutral")
            elif state == "stop":
                logger.info("%s Button stopped listening", self.tag)
                self.trigger_speech_processing()

        elif msg_type == "abort":
            logger.info("%s Received abort", self.tag)
            self.turn_id += 1
            self.is_listening = False
            self.is_processing = False
            self.is_speaking = False
            self.audio_buffer.clear()
            self.last_check_packet_count = 0
            self.vad_tracker.reset()
            await self.send_json({"session_id": self.session_id, "type": "tts", "state": "stop"})
            await self.send_status(self.status.ready, "neutral")
