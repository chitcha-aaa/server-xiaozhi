"""Fake XiaoZhi board for testing server.py without hardware.

Usage:
    python mock_device.py [audio_file] [--turns N] [--url ws://127.0.0.1:8000/ws]

- audio_file: any audio ffmpeg can read (wav/mp3/m4a...). If omitted, a Thai test
  question is generated once with LangMind TTS and cached as test_question.mp3.
- Sends hello -> listen start -> 60 ms Opus frames -> listen stop, like the real
  board in manual (push-to-talk) mode, then waits for tts stop.
- Prints every JSON message from the server, counts received audio frames and
  saves the reply audio to mock_reply_<turn>.wav.
"""
import argparse
import asyncio
import json
import os
import sys
import time

import websockets
import yaml

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.audio_converter import audio_to_opus_packets, opus_packets_to_wav
from app.speech_text import ALLOWED_STATUS

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_QUESTION = "สวัสดีครับ ช่วยแนะนำตัวสั้นๆ หน่อย"


async def make_question_audio() -> bytes:
    cache = os.path.join(HERE, "test_question.mp3")
    if os.path.exists(cache):
        return open(cache, "rb").read()
    from langmind_client import LangMindClient
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))["langmind"]
    client = LangMindClient(cfg["base_url"], cfg["api_key"], cfg["agent_id"], cfg.get("voice_speed", 1.0))
    print(f"[mock] generating test question via LangMind TTS: {DEFAULT_QUESTION}")
    audio = await client.speech(DEFAULT_QUESTION)
    if not audio:
        sys.exit("[mock] could not generate test audio; pass an audio file instead")
    open(cache, "wb").write(audio)
    return audio


async def run(url: str, audio: bytes, turns: int, barge: str | None = None) -> int:
    packets = audio_to_opus_packets(audio, 16000, 60)
    # Add ~1.2 s of trailing silence frames by repeating nothing: real board just stops sending.
    print(f"[mock] {len(packets)} Opus frames ({len(packets) * 0.06:.1f}s) per turn")
    failures = 0
    leaks: list = []
    async with websockets.connect(url, additional_headers={
        "Device-Id": "02:00:00:00:00:01",
        "Client-Id": "mock-device",
        "Protocol-Version": "1",
        **({"Authorization": f"Bearer {os.environ['DEVICE_TOKEN']}"} if os.environ.get("DEVICE_TOKEN") else {}),
    }) as ws:
        await ws.send(json.dumps({
            "type": "hello", "version": 1, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
        }))
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        print("[mock] <-", hello)
        session_id = hello.get("session_id")

        for turn in range(1, turns + 1):
            print(f"\n[mock] ===== turn {turn} =====")
            await ws.send(json.dumps({"session_id": session_id, "type": "listen", "state": "start", "mode": "manual"}))
            for pkt in packets:
                await ws.send(pkt)
                await asyncio.sleep(0.06)
            await ws.send(json.dumps({"session_id": session_id, "type": "listen", "state": "stop"}))
            t0 = time.monotonic()

            reply_frames: list[bytes] = []
            first_audio = None
            got_stop = False
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), 60)
                except asyncio.TimeoutError:
                    print("[mock] TIMEOUT waiting for server (60s)")
                    break
                if isinstance(msg, bytes):
                    if first_audio is None:
                        first_audio = time.monotonic() - t0
                    reply_frames.append(msg)
                    if barge and turn == 1 and len(reply_frames) == 20:
                        if barge == "abort":
                            print("[mock] -> abort (button press) while server is speaking")
                            await ws.send(json.dumps({"session_id": session_id, "type": "abort"}))
                        else:
                            print("[mock] -> barge-in: listen start while server is speaking")
                            await ws.send(json.dumps({"session_id": session_id, "type": "listen", "state": "start"}))
                        await asyncio.sleep(1.5)
                        late = 0
                        saw_stop = False
                        while True:
                            try:
                                m = await asyncio.wait_for(ws.recv(), 0.3)
                            except asyncio.TimeoutError:
                                break
                            if isinstance(m, bytes):
                                late += 1
                            elif json.loads(m).get("state") == "stop":
                                saw_stop = True
                        ok = late <= 30 and (saw_stop or barge != "abort")
                        print(f"[mock] after interrupt: late frames={late}, tts stop={saw_stop} "
                              f"({'OK' if ok else 'FAIL'})")
                        got_stop = ok
                        break
                    continue
                data = json.loads(msg)
                print(f"[mock] <- +{time.monotonic() - t0:5.1f}s {data}")
                # Display policy: only allowlisted status may reach the screen.
                if data.get("type") == "stt" and data.get("text") not in ALLOWED_STATUS:
                    print(f"[mock] FAIL: non-status text sent to screen: {data.get('text')!r}")
                    leaks.append(data)
                if data.get("type") == "tts" and data.get("text"):
                    print(f"[mock] FAIL: tts text sent to screen")
                    leaks.append(data)
                if data.get("type") == "llm" and data.get("text"):
                    print(f"[mock] FAIL: llm text sent to screen")
                    leaks.append(data)
                if data.get("type") == "tts" and data.get("state") == "stop":
                    got_stop = True
                    break

            print(f"[mock] reply audio frames: {len(reply_frames)} ({len(reply_frames) * 0.06:.1f}s), "
                  f"first audio after {first_audio:.1f}s" if first_audio else
                  f"[mock] reply audio frames: 0")
            if reply_frames:
                wav = opus_packets_to_wav(reply_frames, 16000)
                out = os.path.join(HERE, f"mock_reply_{turn}.wav")
                open(out, "wb").write(wav)
                print(f"[mock] saved {out}")
            if not (got_stop and reply_frames):
                failures += 1
    print(f"\n[mock] done: {turns - failures}/{turns} turns got audio + tts stop; "
          f"display leaks: {len(leaks)}")
    return 1 if failures or leaks else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?")
    ap.add_argument("--turns", type=int, default=1)
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    ap.add_argument("--barge", action="store_true", help="interrupt turn 1 with listen start")
    ap.add_argument("--abort", action="store_true", help="interrupt turn 1 with abort (button press)")
    a = ap.parse_args()
    audio = open(a.audio, "rb").read() if a.audio else asyncio.run(make_question_audio())
    mode = "abort" if a.abort else ("listen" if a.barge else None)
    sys.exit(asyncio.run(run(a.url, audio, a.turns, mode)))


if __name__ == "__main__":
    main()
