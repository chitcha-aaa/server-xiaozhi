from dataclasses import dataclass
import math
import struct
import subprocess
import logging
import webrtcvad

logger = logging.getLogger("AudioConverter")
FRAME_MS = 20              # webrtcvad frame size used here (640 bytes @ 16 kHz)


@dataclass(frozen=True)
class VadConfig:
    """Per-board end-of-speech VAD tuning.

    Defaults are tuned for board 2 (lynai, RMS 3800-8800, room noise floor ~900).
    Quiet-mic boards like board 1 (zuowei, RMS 400-1200) can pass their own
    VadConfig (e.g. trailing_silence_ms=800, min_loud_speech_ms=600,
    trailing_rel_level=0.15, aggressiveness=2).
    """
    trailing_silence_ms: int = 500
    min_loud_speech_ms: int = 240
    trailing_rel_level: float = 0.25
    trailing_min_rms: float = 150.0
    aggressiveness: int = 3

    @property
    def trailing_frames(self) -> int:
        return max(1, int(self.trailing_silence_ms) // FRAME_MS)

    @property
    def min_loud_speech_frames(self) -> int:
        return max(1, int(self.min_loud_speech_ms) // FRAME_MS)

    @property
    def clamped_aggressiveness(self) -> int:
        return max(0, min(3, int(self.aggressiveness)))


DEFAULT_VAD_CONFIG = VadConfig()


def configure(trailing_silence_ms: int = 0, min_loud_speech_ms: int = 0,
              trailing_rel_level: float = 0.0, trailing_min_rms: float = 0.0,
              aggressiveness: int = 0) -> VadConfig:
    """Updates DEFAULT_VAD_CONFIG (backward-compatible helper). Prefer passing VadConfig directly."""
    global DEFAULT_VAD_CONFIG
    cur = DEFAULT_VAD_CONFIG
    DEFAULT_VAD_CONFIG = VadConfig(
        trailing_silence_ms=int(trailing_silence_ms) if trailing_silence_ms else cur.trailing_silence_ms,
        min_loud_speech_ms=int(min_loud_speech_ms) if min_loud_speech_ms else cur.min_loud_speech_ms,
        trailing_rel_level=float(trailing_rel_level) if trailing_rel_level else cur.trailing_rel_level,
        trailing_min_rms=float(trailing_min_rms) if trailing_min_rms else cur.trailing_min_rms,
        aggressiveness=int(aggressiveness) if aggressiveness else cur.aggressiveness,
    )
    logger.info("Default VAD end-of-speech: trailing %d ms, min loud speech %d ms, rel level %.2f, "
                "min rms %.0f, aggressiveness %d",
                DEFAULT_VAD_CONFIG.trailing_frames * FRAME_MS,
                DEFAULT_VAD_CONFIG.min_loud_speech_frames * FRAME_MS,
                DEFAULT_VAD_CONFIG.trailing_rel_level,
                DEFAULT_VAD_CONFIG.trailing_min_rms,
                DEFAULT_VAD_CONFIG.clamped_aggressiveness)
    return DEFAULT_VAD_CONFIG

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


def opus_packets_to_pcm(packets: list[bytes], sample_rate: int = 16000) -> bytes:
    """Decodes raw Opus packets (from XiaoZhi ESP32) to headerless 16-bit mono PCM."""
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
        return res.stdout
    logger.error("FFmpeg Opus-to-WAV conversion failed")
    return b""


def opus_packets_to_wav(packets: list[bytes], sample_rate: int = 16000) -> bytes:
    """Takes a list of raw Opus audio packets (from XiaoZhi ESP32) and converts them to WAV bytes."""
    pcm = opus_packets_to_pcm(packets, sample_rate)
    return _pcm16_to_wav(pcm, sample_rate) if pcm else b""


def audio_to_opus_packets(audio_bytes: bytes, sample_rate: int = 16000, frame_duration_ms: int = 60,
                          encode_frame_ms: int = 0, bitrate: str = "24k") -> list[bytes]:
    """Takes MP3 or WAV bytes (from LangMind TTS) and converts them to a list of raw Opus packets.
    Each packet covers frame_duration_ms. With encode_frame_ms set (e.g. 20 for 60 ms packets),
    frames are encoded at that size and several are joined per packet."""
    if not audio_bytes:
        return []
    enc_ms = encode_frame_ms or frame_duration_ms
    pack_frames = frame_duration_ms // enc_ms

    try:
        res = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                "-c:a", "libopus",
                "-ar", str(sample_rate),
                "-ac", "1",
                "-b:a", bitrate,
                "-application", "voip",
                "-frame_duration", str(enc_ms),
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
    packets = packets[2:]
    if pack_frames > 1:
        packets = _pack_code3(packets, pack_frames)
    return packets


def _opus_len_bytes(n: int) -> bytes:
    if n < 252:
        return bytes([n])
    b0 = 252 + ((n - 252) & 3)
    return bytes([b0, (n - b0) >> 2])


def _pack_code3(frames: list[bytes], per_packet: int) -> list[bytes]:
    """Join single-frame Opus packets (TOC code 0) into code-3 packets holding `per_packet`
    frames each (RFC 6716 3.2.5), the layout the 1.4.7 board itself sends: 3 x 20 ms SILK."""
    out = []
    for i in range(0, len(frames), per_packet):
        group = frames[i:i + per_packet]
        toc = group[0][0]
        assert all(f[0] == toc for f in group), "frames in one packet must share a TOC"
        bodies = [f[1:] for f in group]
        vbr = len({len(b) for b in bodies}) > 1
        pkt = bytearray([(toc & 0xFC) | 3, (0x80 if vbr else 0) | len(group)])
        if vbr:
            for b in bodies[:-1]:
                pkt += _opus_len_bytes(len(b))
        for b in bodies:
            pkt += b
        out.append(bytes(pkt))
    return out


def _frame_stats(pcm: bytes, vad: webrtcvad.Vad, sample_rate: int = 16000) -> tuple[list[bool], list[float]]:
    """Per 20 ms frame: webrtcvad flag and RMS. Trailing bytes shorter than a frame are ignored."""
    frame_bytes = (sample_rate * FRAME_MS // 1000) * 2
    samples_per_frame = frame_bytes // 2
    fmt = f"<{samples_per_frame}h"
    vad_flags, rms = [], []
    for i in range(0, (len(pcm) // frame_bytes) * frame_bytes, frame_bytes):
        frame = pcm[i : i + frame_bytes]
        vad_flags.append(vad.is_speech(frame, sample_rate))
        fs = struct.unpack(fmt, frame)
        rms.append(math.sqrt(sum(x * x for x in fs) / samples_per_frame))
    return vad_flags, rms


def _analyze_frames(frame_vad: list[bool], frame_rms: list[float],
                    config: VadConfig | None = None) -> dict:
    """Shared end-of-speech decision over already-measured 20 ms frames."""
    cfg = config or DEFAULT_VAD_CONFIG
    num_frames = len(frame_vad)
    if not num_frames:
        return {"has_speech": False, "trailing_silent": True, "rms": 0.0}

    total_sq = sum(r * r for r in frame_rms)
    rms = math.sqrt(total_sq / num_frames)
    speech_frames = sum(frame_vad)

    # A trailing frame only counts as speech when it is also loud compared with this
    # utterance's own speech level: VAD alone keeps calling faint sound (room talk, echo)
    # speech, so the end of an utterance was missed and every turn waited for the 10 s cap.
    voiced = sorted(r for r, v in zip(frame_rms, frame_vad) if v)
    speech_level = voiced[(len(voiced) * 9) // 10] if voiced else 0.0  # 90th percentile = the loud part
    loud_enough = max(cfg.trailing_min_rms, cfg.trailing_rel_level * speech_level)
    trailing_frames = min(cfg.trailing_frames, num_frames)
    trailing_speech = sum(
        1 for v, r in zip(frame_vad[-trailing_frames:], frame_rms[-trailing_frames:]) if v and r >= loud_enough
    )
    trailing_speech_ratio = trailing_speech / trailing_frames
    overall_speech_ratio = speech_frames / num_frames

    # Loudness alone is not speech: a fan/room noise at RMS ~800 used to trigger empty STT
    # calls every second. Require a real VAD speech share, and more of it when it's quiet.
    has_speech = (overall_speech_ratio >= 0.30 and rms >= 300) or (overall_speech_ratio >= 0.15 and rms >= 1500)
    # Don't end the turn on a pause until the user has really spoken.
    loud_speech_frames = sum(1 for v, r in zip(frame_vad, frame_rms) if v and r >= loud_enough)
    trailing_silent = trailing_speech_ratio < 0.20 and loud_speech_frames >= cfg.min_loud_speech_frames

    return {
        "rms": rms,
        "overall_speech_ratio": overall_speech_ratio,
        "trailing_speech_ratio": trailing_speech_ratio,
        "has_speech": has_speech,
        "trailing_silent": trailing_silent,
        "frames": num_frames,
        "loud_speech_frames": loud_speech_frames,
        "loud_enough": loud_enough,
        # Noise floor vs speech level: if these are close, the mic is hearing steady noise
        # rather than speech, and no trailing threshold can separate the two.
        "floor": sorted(frame_rms)[len(frame_rms) // 10],
        "speech_level": speech_level,
    }


class VadTracker:
    """Incremental voice activity over a growing list of Opus packets.

    Each session gets its own VadTracker with its own VadConfig and webrtcvad.Vad
    instance, so concurrent boards with different mic profiles never interfere.
    """

    def __init__(self, sample_rate: int = 16000, config: VadConfig | None = None):
        self.sample_rate = sample_rate
        self.config = config or DEFAULT_VAD_CONFIG
        self._vad = webrtcvad.Vad(self.config.clamped_aggressiveness)
        self._frame_bytes = (sample_rate * FRAME_MS // 1000) * 2
        self.consumed = 0
        self.frame_vad: list[bool] = []
        self.frame_rms: list[float] = []
        self._tail = b""  # PCM left over from the last decode, shorter than one 20 ms frame

    def reset(self) -> None:
        self.consumed = 0
        self.frame_vad.clear()
        self.frame_rms.clear()
        self._tail = b""

    def feed_pcm(self, pcm: bytes) -> None:
        """Processes raw 16-bit mono PCM bytes directly (also useful for tests)."""
        if not pcm:
            return
        buf = self._tail + pcm
        used = (len(buf) // self._frame_bytes) * self._frame_bytes
        self._tail = buf[used:]
        if used:
            vad, rms = _frame_stats(buf[:used], self._vad, self.sample_rate)
            self.frame_vad.extend(vad)
            self.frame_rms.extend(rms)

    def feed(self, packets: list[bytes]) -> bool:
        """Decodes the packets added since the last call. False if decoding failed."""
        new = packets[self.consumed:]
        if not new:
            return True
        pcm = opus_packets_to_pcm(new, self.sample_rate)
        if not pcm:
            return False
        self.consumed = len(packets)
        self.feed_pcm(pcm)
        return True

    def analyze(self) -> dict:
        return _analyze_frames(self.frame_vad, self.frame_rms, self.config)


def check_voice_activity(wav_bytes: bytes, config: VadConfig | None = None,
                         sample_rate: int = 16000) -> dict:
    """
    Analyzes mono 16-bit WAV bytes for:
    1. Overall RMS energy (volume)
    2. WebRTC VAD speech ratio
    3. Trailing silence (has the speaker paused at the end)
    """
    cfg = config or DEFAULT_VAD_CONFIG
    frame_bytes = (sample_rate * FRAME_MS // 1000) * 2
    if len(wav_bytes) <= 44 + frame_bytes:
        return {"has_speech": False, "trailing_silent": True, "rms": 0.0}
    pcm = wav_bytes[44:]
    num_samples = len(pcm) // 2
    if num_samples == 0:
        return {"has_speech": False, "trailing_silent": True, "rms": 0.0}

    vad = webrtcvad.Vad(cfg.clamped_aggressiveness)
    frame_vad, frame_rms = _frame_stats(pcm, vad, sample_rate)
    return _analyze_frames(frame_vad, frame_rms, cfg)
