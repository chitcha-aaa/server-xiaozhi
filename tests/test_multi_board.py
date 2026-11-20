"""Local multi-board test suite (up to 10 concurrent heterogeneous boards)."""
import asyncio
from io import BytesIO
import json
import math
import struct
import threading
import wave

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.audio_converter import VadConfig, VadTracker, audio_to_opus_packets
from app.devices import DeviceRegistry
from app.server import create_app
from app import speech_text as st


def _make_tone_wav(duration_sec: float = 0.6, freq: float = 440.0, amp: int = 8000, rate: int = 16000) -> bytes:
    """Generates a mono 16-bit PCM WAV with a speech-like modulated tone."""
    n = int(rate * duration_sec)
    samples = []
    for i in range(n):
        t = i / rate
        # Mix two frequencies + slight envelope so it has clear RMS energy
        val = int(amp * (0.6 * math.sin(2 * math.pi * freq * t) + 0.4 * math.sin(2 * math.pi * (freq * 1.5) * t)))
        samples.append(max(-32768, min(32767, val)))
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{n}h", *samples))
    return buf.getvalue()


class FakeLangMind:
    """In-memory LangMind adapter for fast, deterministic multi-board tests."""

    def __init__(self, reply_wav: bytes):
        self.reply_wav = reply_wav
        self.stt_calls = 0
        self.chat_calls = 0
        self.tts_calls = 0
        self.conv_counter = 0

    async def aclose(self) -> None:
        pass

    async def transcribe(self, wav_bytes: bytes) -> str:
        self.stt_calls += 1
        await asyncio.sleep(0.01)
        return f"hello from turn {self.stt_calls}"

    async def chat_stream(self, query: str, conversation_id: str | None = None):
        self.chat_calls += 1
        conv_id = conversation_id
        if not conv_id:
            self.conv_counter += 1
            conv_id = f"conv-{self.conv_counter}"
        yield ("conv", conv_id)
        await asyncio.sleep(0.01)
        yield ("text", "Hello there, ")
        await asyncio.sleep(0.01)
        yield ("text", "this is a short reply.")

    async def speech(self, text: str) -> bytes:
        self.tts_calls += 1
        await asyncio.sleep(0.01)
        return self.reply_wav


def _ten_board_config(tmp_path) -> tuple[dict, dict[str, str]]:
    """Builds a 10-board config across 3 distinct hardware profiles."""
    tokens = {f"board{i}": f"{i:02d}" + ("a" * 30) for i in range(1, 11)}
    devices = {}
    for i in range(1, 11):
        profile = "default" if i <= 4 else ("lynai" if i <= 8 else "wide_s3")
        devices[f"board{i}"] = {
            "mac": f"aa:bb:cc:dd:00:{i:02x}",
            "profile": profile,
        }
    cfg = {
        "server": {
            "port": 8000,
            "snapshot_dir": str(tmp_path),
            "max_ffmpeg_workers": 4,
        },
        "langmind": {
            "base_url": "https://langmind.in.th/api",
            "agent_id": "shared-agent-id",
        },
        "profiles": {
            "default": {
                "audio": {"sample_rate": 16000, "frame_duration_ms": 60, "tts_encode_frame_ms": 20},
                "vad": {"trailing_silence_ms": 800, "min_loud_speech_ms": 600, "trailing_rel_level": 0.15, "aggressiveness": 2},
                "screen": {"status_channel": "stt", "screen_image": False, "size": [240, 240], "mcp": False},
            },
            "lynai": {
                "vad": {"trailing_silence_ms": 500, "min_loud_speech_ms": 400, "trailing_rel_level": 0.35, "aggressiveness": 3},
                "screen": {"status_channel": "tts", "screen_image": True, "size": [240, 240], "mcp": True},
            },
            "wide_s3": {
                "audio": {"frame_duration_ms": 60, "tts_encode_frame_ms": 20},
                "vad": {"trailing_silence_ms": 600, "min_loud_speech_ms": 450, "trailing_rel_level": 0.25, "aggressiveness": 2},
                "screen": {"status_channel": "both", "screen_image": True, "size": [320, 240], "mcp": True},
            },
        },
        "devices": devices,
    }
    env = {
        "APP_ENV": "development",
        "LANGMIND_API_KEY": "test-key",
        "LANGMIND_AGENT_ID": "shared-agent-id",
        "DEVICE_TOKENS": ",".join(f"{name}:{tok}" for name, tok in tokens.items()),
    }
    return cfg, env


# --- 1. DeviceRegistry & Profile Tests ----------------------------------------

def test_registry_legacy_config_and_env_compat():
    tok1 = "1" * 32
    tok2 = "2" * 32
    cfg = {
        "devices": {"board2": tok2},
        "device_ids": {"44:bd:8d:7b:39:50": "board2"},
        "server": {"status_channel": "tts", "screen_image": True},
    }
    env = {"DEVICE_TOKEN": tok1}
    reg = DeviceRegistry.from_config(cfg, env)

    assert reg.has_tokens()
    assert reg.ota_token_for("44:BD:8D:7B:39:50") == tok2
    assert reg.ota_token_for("unknown-mac") == tok1

    ctx1 = reg.authenticate(tok1, None)
    ctx2 = reg.authenticate(tok2, None)
    assert ctx1 is not None and ctx2 is not None
    # No "authenticated-device" collision when Device-Id header is omitted
    assert ctx1.device_id == "board1"
    assert ctx2.device_id == "board2"
    assert ctx1.device_id != ctx2.device_id
    assert reg.authenticate("wrong" * 8, "44:bd:8d:7b:39:50") is None


def test_registry_ten_heterogeneous_boards(tmp_path):
    cfg, env = _ten_board_config(tmp_path)
    reg = DeviceRegistry.from_config(cfg, env)

    for i in range(1, 11):
        mac = f"aa:bb:cc:dd:00:{i:02x}"
        expected_tok = f"{i:02d}" + ("a" * 30)
        assert reg.ota_token_for(mac) == expected_tok

        ctx = reg.authenticate(expected_tok, mac)
        assert ctx is not None
        assert ctx.name == f"board{i}"
        assert ctx.device_id == mac
        if i <= 4:
            assert ctx.profile.name == "default"
            assert ctx.profile.vad.trailing_silence_ms == 800
            assert ctx.profile.screen.status_channel == "stt"
        elif i <= 8:
            assert ctx.profile.name == "lynai"
            assert ctx.profile.vad.trailing_silence_ms == 500
            assert ctx.profile.screen.status_channel == "tts"
            assert ctx.profile.screen.screen_image is True
        else:
            assert ctx.profile.name == "wide_s3"
            assert ctx.profile.screen.size == (320, 240)
            assert ctx.profile.screen.status_channel == "both"
        # All status texts default to English
        assert ctx.profile.status.listening == "Listening..."
        assert ctx.profile.status.thinking == "Thinking..."
        assert ctx.profile.status.speaking == "Speaking"


# --- 2. Per-Session VadTracker Isolation --------------------------------------

def test_vad_trackers_isolated_across_configs():
    quiet_cfg = VadConfig(trailing_silence_ms=800, min_loud_speech_ms=600, trailing_rel_level=0.15, aggressiveness=1)
    loud_cfg = VadConfig(trailing_silence_ms=300, min_loud_speech_ms=200, trailing_rel_level=0.50, aggressiveness=3)

    t_quiet = VadTracker(16000, quiet_cfg)
    t_loud = VadTracker(16000, loud_cfg)

    # Synthetic frames: 15 frames loud speech (300ms) + 20 frames low noise (400ms)
    t_quiet.frame_vad = [True] * 15 + [True] * 20
    t_quiet.frame_rms = [4000.0] * 15 + [1000.0] * 20  # 1000 is 25% of 4000

    t_loud.frame_vad = list(t_quiet.frame_vad)
    t_loud.frame_rms = list(t_quiet.frame_rms)

    res_quiet = t_quiet.analyze()
    res_loud = t_loud.analyze()

    # For quiet_cfg (rel=0.15 -> thr=600, min_loud=30 frames): 1000 >= 600 so trailing is NOT silent
    assert res_quiet["trailing_silent"] is False
    # For loud_cfg (rel=0.50 -> thr=2000, min_loud=10 frames, trailing=15 frames): 1000 < 2000 so trailing IS silent!
    assert res_loud["trailing_silent"] is True


# --- 3. HTTP Endpoints: OTA, Screen Images, Snapshot Upload -------------------

def test_ota_screen_images_and_snapshot_upload(tmp_path):
    cfg, env = _ten_board_config(tmp_path)
    app = create_app(config=cfg, env=env, langmind_client=FakeLangMind(b""))
    with TestClient(app) as client:
        assert client.get("/healthz").json()["status"] == "ready"

        # OTA gives each board its own token
        r1 = client.post("/ota/", headers={"Device-Id": "aa:bb:cc:dd:00:01"})
        r5 = client.post("/ota/", headers={"Device-Id": "aa:bb:cc:dd:00:05"})
        assert r1.status_code == 200 and r5.status_code == 200
        assert r1.json()["websocket"]["token"] == "01" + ("a" * 30)
        assert r5.json()["websocket"]["token"] == "05" + ("a" * 30)

        # Profile-specific screen PNG sizes
        img_lynai = client.get("/screen/lynai/thinking.png")
        img_wide = client.get("/screen/wide_s3/thinking.png")
        assert img_lynai.status_code == 200 and img_wide.status_code == 200
        assert Image.open(BytesIO(img_lynai.content)).size == (240, 240)
        assert Image.open(BytesIO(img_wide.content)).size == (320, 240)
        assert client.get("/screen/unknown_profile/thinking.png").status_code == 404
        assert client.get("/screen/lynai/not_a_slot.png").status_code == 404

        # Concurrent snapshot uploads do not overwrite each other
        u1 = client.post("/screen/upload?device=board2", files={"file": ("a.jpg", b"jpeg-one", "image/jpeg")})
        u2 = client.post("/screen/upload?device=board3", files={"file": ("b.jpg", b"jpeg-two", "image/jpeg")})
        assert u1.status_code == 200 and u2.status_code == 200
        assert u1.json()["saved"] != u2.json()["saved"]
        assert "board2" in u1.json()["saved"]
        assert "board3" in u2.json()["saved"]


# --- 4. Ten Concurrent Heterogeneous Boards End-to-End ------------------------

def test_ten_concurrent_boards_full_turn(tmp_path):
    """Spins up 10 concurrent board clients across 3 profiles and runs a full voice turn each."""
    cfg, env = _ten_board_config(tmp_path)
    reply_wav = _make_tone_wav(duration_sec=0.25, freq=520.0, amp=6000)
    # Use test_question.mp3 or synthetic tone encoded to Opus packets
    user_wav = _make_tone_wav(duration_sec=0.6, freq=300.0, amp=9000)
    user_opus = audio_to_opus_packets(user_wav, 16000, 60, 20, "16k")
    assert len(user_opus) >= 8, "Need at least 8 Opus packets for a valid speech turn"

    fake_lm = FakeLangMind(reply_wav=reply_wav)
    app = create_app(config=cfg, env=env, langmind_client=fake_lm)

    # Bypass WebRTC VAD speech-vs-tone classification by monkeypatching check_voice_activity
    # for this pure-tone synthetic test input (we already test VAD directly above).
    from app import session as session_mod
    orig_cva = session_mod.check_voice_activity
    session_mod.check_voice_activity = lambda wav, cfg=None, sr=16000: {
        "has_speech": True, "trailing_silent": True, "rms": 5000.0
    }

    results: dict[int, dict] = {}
    errors: list[Exception] = []

    def run_board(client: TestClient, idx: int):
        try:
            tok = f"{idx:02d}" + ("a" * 30)
            mac = f"aa:bb:cc:dd:00:{idx:02x}"
            headers = {"Authorization": f"Bearer {tok}", "Device-Id": mac}
            with client.websocket_connect("/ws", headers=headers) as ws:
                ws.send_text(json.dumps({
                    "type": "hello",
                    "version": 1,
                    "transport": "websocket",
                    "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
                }))
                hello = json.loads(ws.receive_text())
                assert hello["type"] == "hello"
                sid = hello["session_id"]

                # Simulate MCP tools advertisement on lynai / wide_s3 boards
                if idx >= 5:
                    ws.send_text(json.dumps({
                        "type": "mcp",
                        "payload": {
                            "jsonrpc": "2.0",
                            "id": 102,
                            "result": {"tools": [{"name": "self.screen.preview_image"}]},
                        },
                    }))

                ws.send_text(json.dumps({"session_id": sid, "type": "listen", "state": "start"}))
                for pkt in user_opus:
                    ws.send_bytes(pkt)
                ws.send_text(json.dumps({"session_id": sid, "type": "listen", "state": "stop"}))

                json_msgs = []
                audio_frames = 0
                for _ in range(200):
                    msg = ws.receive()
                    if msg.get("bytes"):
                        audio_frames += 1
                    elif msg.get("text"):
                        data = json.loads(msg["text"])
                        json_msgs.append(data)
                        if data.get("type") == "tts" and data.get("state") == "stop":
                            break

                results[idx] = {
                    "session_id": sid,
                    "json_msgs": json_msgs,
                    "audio_frames": audio_frames,
                }
        except Exception as exc:
            errors.append(exc)

    try:
        with TestClient(app) as client:
            threads = [threading.Thread(target=run_board, args=(client, i)) for i in range(1, 11)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                assert not t.is_alive(), "Board thread timed out"
    finally:
        session_mod.check_voice_activity = orig_cva

    assert not errors, f"Board errors: {errors}"
    assert len(results) == 10
    assert len({r["session_id"] for r in results.values()}) == 10
    assert fake_lm.stt_calls == 10
    assert fake_lm.chat_calls == 10

    for idx, res in results.items():
        assert res["audio_frames"] > 0, f"board{idx} received no TTS audio"
        msgs = res["json_msgs"]

        # Verify display allowlist (English only, zero transcript leaks)
        stt_texts = [m["text"] for m in msgs if m.get("type") == "stt"]
        tts_texts = [m["text"] for m in msgs if m.get("type") == "tts" and m.get("state") == "sentence_start"]
        for t in stt_texts + tts_texts:
            assert t in st.DEFAULT_STATUS_CATALOG.allowed

        # Verify per-model screen channel isolation
        if idx <= 4:  # default: stt only, no MCP preview_image
            assert len(stt_texts) > 0
            assert len(tts_texts) == 0
            assert not any(
                m.get("type") == "mcp" and m.get("payload", {}).get("method") == "tools/call"
                for m in msgs
            )
        elif idx <= 8:  # lynai: tts only + MCP preview_image to /screen/lynai/...
            assert len(stt_texts) == 0
            assert len(tts_texts) > 0
            preview_urls = [
                m["payload"]["params"]["arguments"]["url"]
                for m in msgs
                if m.get("type") == "mcp"
                and m.get("payload", {}).get("method") == "tools/call"
                and m["payload"]["params"].get("name") == "self.screen.preview_image"
            ]
            assert preview_urls and all("/screen/lynai/" in u for u in preview_urls)
        else:  # wide_s3: both channels + MCP preview_image to /screen/wide_s3/...
            assert len(stt_texts) > 0 and len(tts_texts) > 0
            preview_urls = [
                m["payload"]["params"]["arguments"]["url"]
                for m in msgs
                if m.get("type") == "mcp"
                and m.get("payload", {}).get("method") == "tools/call"
                and m["payload"]["params"].get("name") == "self.screen.preview_image"
            ]
            assert preview_urls and all("/screen/wide_s3/" in u for u in preview_urls)


def test_reconnect_and_ota_conversation_isolation(tmp_path):
    """Reconnecting or rebooting (/ota) one board must not affect another board."""
    cfg, env = _ten_board_config(tmp_path)
    app = create_app(config=cfg, env=env, langmind_client=FakeLangMind(b""))

    tok1 = "01" + ("a" * 30)
    tok2 = "02" + ("a" * 30)
    mac1 = "aa:bb:cc:dd:00:01"
    mac2 = "aa:bb:cc:dd:00:02"

    # Seed conversations for both boards
    app.state.sessions.put_conversation(mac1, "conv-board-1")
    app.state.sessions.put_conversation(mac2, "conv-board-2")

    with TestClient(app) as client:
        # Wrong token is rejected
        with pytest.raises(Exception):
            with client.websocket_connect("/ws", headers={"Authorization": "Bearer " + "x" * 32, "Device-Id": mac1}):
                pass

        # Calling /ota for board1 clears ONLY board1's conversation
        res = client.post("/ota/", headers={"Device-Id": mac1})
        assert res.status_code == 200
        assert app.state.sessions.get_conversation(mac1) is None
        assert app.state.sessions.get_conversation(mac2) == "conv-board-2"

        # Two boards connected at the same time -> healthz reports 2 active devices
        with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {tok1}", "Device-Id": mac1}):
            with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {tok2}", "Device-Id": mac2}):
                assert client.get("/healthz").json()["active_devices"] == 2
                # Reconnecting board1 does not remove board2
                with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {tok1}", "Device-Id": mac1}):
                    assert mac2 in app.state.sessions.active_device_ids()
                    assert mac1 in app.state.sessions.active_device_ids()

