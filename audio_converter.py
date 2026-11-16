import math
import struct
import subprocess
import logging
import webrtcvad

logger = logging.getLogger("AudioConverter")
_vad = webrtcvad.Vad(2)
# End-of-speech = this much trailing silence. 25 x 20 ms = 500 ms (was 700 ms).
TRAILING_FRAMES = 25

# Precomputed CRC table for Ogg
_CRC_LOOKUP = [0] * 256
for _i in range(256):
    _curr = _i << 24
    for _ in range(8):
        if _curr & 0x80000000:
            _curr = ((_curr << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            _curr = (_curr << 1) & 0xFFFFFFFF
    _CRC_LOOKUP[_i] = _curr


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC_LOOKUP[((crc >> 24) & 0xFF) ^ b]) & 0xFFFFFFFF
    return crc


def make_ogg_page(header_type: int, granule_pos: int, serial: int, seq: int, packets: list[bytes]) -> bytes:
    """Creates a valid Ogg page containing one or more packets."""
    body = b"".join(packets)
    segments = []
    for p in packets:
        plen = len(p)
        while plen >= 255:
            segments.append(255)
            plen -= 255
        segments.append(plen)

    header = bytearray(27 + len(segments))
    header[0:4] = b"OggS"
    header[4] = 0
    header[5] = header_type
    struct.pack_into("<q", header, 6, granule_pos)
    struct.pack_into("<I", header, 14, serial)
    struct.pack_into("<I", header, 18, seq)
    header[22:26] = b"\x00\x00\x00\x00"
    header[26] = len(segments)
    header[27:27 + len(segments)] = bytes(segments)

    crc = _ogg_crc(header + body)
    struct.pack_into("<I", header, 22, crc)
    return bytes(header) + body


def _pcm16_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw 16-bit mono PCM bytes in a canonical 44-byte WAV header with exact sizes."""
    data_size = len(pcm_data)
    byte_rate = sample_rate * 2  # mono 16-bit
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,           # PCM fmt chunk size
        1,            # AudioFormat = 1 (PCM)
        1,            # NumChannels = 1 (mono)
        sample_rate,
        byte_rate,
        2,            # BlockAlign = 2 bytes
        16,           # BitsPerSample = 16
        b"data",
        data_size,
    )
    return header + pcm_data


def opus_packets_to_wav(packets: list[bytes], sample_rate: int = 16000) -> bytes:
    """Takes a list of raw Opus audio packets (from XiaoZhi ESP32) and converts them to WAV bytes."""
    if not packets:
        return b""

    opus_head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 0, sample_rate, 0, 0)
    vendor = b"XiaoZhi-LangMind"
    opus_tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)

    serial = 0x5849414F  # 'XIAO'
    pages = [
        make_ogg_page(0x02, 0, serial, 0, [opus_head]),
        make_ogg_page(0x00, 0, serial, 1, [opus_tags]),
    ]

    granule = 0
    samples_per_frame = int(sample_rate * 0.060)  # 60ms = 960 samples @ 16kHz
    for i, p in enumerate(packets):
        granule += samples_per_frame
        flags = 0x04 if i == len(packets) - 1 else 0x00  # 0x04 is EOS
        pages.append(make_ogg_page(flags, granule, serial, 2 + i, [p]))

    ogg_data = b"".join(pages)

    try:
        res = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "ogg", "-i", "pipe:0",
             "-ar", str(sample_rate), "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"],
            input=ogg_data,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("FFmpeg Opus-to-WAV conversion failed (%s)", type(exc).__name__)
        return b""
    if res.returncode == 0 and res.stdout:
        return _pcm16_to_wav(res.stdout, sample_rate)
    logger.error("FFmpeg Opus-to-WAV conversion failed")
    return b""


def audio_to_opus_packets(audio_bytes: bytes, sample_rate: int = 16000, frame_duration_ms: int = 60) -> list[bytes]:
    """Takes MP3 or WAV bytes (from LangMind TTS) and converts them to a list of raw Opus packets (60ms each)."""
    if not audio_bytes:
        return []

    try:
        res = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-c:a", "libopus",
                "-ar", str(sample_rate),
                "-ac", "1",
                "-b:a", "24k",
                "-frame_duration", str(frame_duration_ms),
                "-f", "ogg",
                "pipe:1",
            ],
            input=audio_bytes,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("FFmpeg audio-to-Opus conversion failed (%s)", type(exc).__name__)
        return []
    if res.returncode != 0 or not res.stdout:
        logger.error("FFmpeg audio-to-Opus conversion failed")
        return []

    # Parse Ogg Opus packets
    data = res.stdout
    pos = 0
    packets = []
    current_packet = bytearray()
    while pos < len(data):
        if data[pos:pos + 4] != b"OggS" or pos + 27 > len(data):
            break
        num_segments = data[pos + 26]
        seg_end = pos + 27 + num_segments
        if seg_end > len(data):
            break
        seg_table = data[pos + 27:seg_end]
        body_pos = seg_end
        for seg_len in seg_table:
            next_pos = body_pos + seg_len
            if next_pos > len(data):
                return []
            current_packet.extend(data[body_pos:next_pos])
            body_pos = next_pos
            if seg_len < 255:
                packets.append(bytes(current_packet))
                current_packet = bytearray()
        pos = body_pos

    # Packets[0] is OpusHead, Packets[1] is OpusTags, Packets[2:] are the audio packets
    return packets[2:]


def check_voice_activity(wav_bytes: bytes) -> dict:
    """
    Analyzes 16kHz mono WAV bytes for:
    1. Overall RMS energy (volume)
    2. WebRTC VAD speech ratio
    3. Trailing silence (has the speaker paused at the end)
    """
    if len(wav_bytes) <= 44 + 640:
        return {"has_speech": False, "trailing_silent": True, "rms": 0.0}
    pcm = wav_bytes[44:]
    num_samples = len(pcm) // 2
    if num_samples == 0:
        return {"has_speech": False, "trailing_silent": True, "rms": 0.0}

    samples = struct.unpack(f"<{num_samples}h", pcm[: num_samples * 2])
    rms = math.sqrt(sum(s * s for s in samples) / num_samples)

    num_frames = len(pcm) // 640
    speech_frames = 0
    for i in range(0, num_frames * 640, 640):
        if _vad.is_speech(pcm[i : i + 640], 16000):
            speech_frames += 1

    # Check trailing ~700ms (last 35 frames of 20ms)
    trailing_frames = min(TRAILING_FRAMES, num_frames)  # 20 ms frames
    trailing_pcm = pcm[-trailing_frames * 640 :]
    trailing_speech = sum(
        1 for i in range(0, trailing_frames * 640, 640) if _vad.is_speech(trailing_pcm[i : i + 640], 16000)
    )
    trailing_speech_ratio = trailing_speech / trailing_frames if trailing_frames > 0 else 0
    overall_speech_ratio = speech_frames / num_frames if num_frames > 0 else 0

    # Loudness alone is not speech: a fan/room noise at RMS ~800 used to trigger empty STT calls
    # every second. Require a real VAD speech share, and more of it when it's quiet.
    has_speech = (overall_speech_ratio >= 0.30 and rms >= 300) or (overall_speech_ratio >= 0.15 and rms >= 1500)
    trailing_silent = trailing_speech_ratio < 0.20

    return {
        "rms": rms,
        "overall_speech_ratio": overall_speech_ratio,
        "trailing_speech_ratio": trailing_speech_ratio,
        "has_speech": has_speech,
        "trailing_silent": trailing_silent,
        "frames": num_frames,
    }
