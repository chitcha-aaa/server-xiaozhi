import math
import struct
import subprocess
import logging
import webrtcvad

logger = logging.getLogger("AudioConverter")
_vad = webrtcvad.Vad(2)
FRAME_MS = 20              # webrtcvad frame size used here (640 bytes @ 16 kHz)
# End-of-speech = this much trailing silence. Tuned for board 2 (lynai), whose mic runs
# RMS 3800-8800; board 1 (zuowei, RMS 400-1200) may need the old 800/600 ms back, so both
# are overridable from config.yaml via configure() below.
TRAILING_FRAMES = 25           # 500 ms pause ends the turn
MIN_LOUD_SPEECH_FRAMES = 20    # need >= 0.4 s of loud voiced frames before a pause may end the turn
TRAILING_MIN_RMS = 150.0   # a trailing frame quieter than this is silence
# ...or quieter than this share of the loud (90th percentile) speech-frame RMS. 0.15 was set
# for board 1's quiet mic. On board 2 the room noise floor sits around RMS 900 while speech
# reaches 2900-8000, and webrtcvad still calls that noise speech 60-70% of the time, so the
# bar landed at ~940 and the trailing window never went quiet: every turn ran to the 10 s cap.
TRAILING_REL_LEVEL = 0.35


def configure(trailing_silence_ms: int = 0, min_loud_speech_ms: int = 0,
              trailing_rel_level: float = 0.0, trailing_min_rms: float = 0.0,
              aggressiveness: int = 0) -> None:
    """Overrides the end-of-speech tuning from config/env. 0 keeps the default."""
    global TRAILING_FRAMES, MIN_LOUD_SPEECH_FRAMES, TRAILING_REL_LEVEL, TRAILING_MIN_RMS, _vad
    if trailing_silence_ms:
        TRAILING_FRAMES = max(1, int(trailing_silence_ms) // FRAME_MS)
    if min_loud_speech_ms:
        MIN_LOUD_SPEECH_FRAMES = max(1, int(min_loud_speech_ms) // FRAME_MS)
    if trailing_rel_level:
        TRAILING_REL_LEVEL = float(trailing_rel_level)
    if trailing_min_rms:
        TRAILING_MIN_RMS = float(trailing_min_rms)
    if aggressiveness:
        _vad = webrtcvad.Vad(max(0, min(3, int(aggressiveness))))
    logger.info("VAD end-of-speech: trailing %d ms, min loud speech %d ms, rel level %.2f, "
                "min rms %.0f, aggressiveness %d",
                TRAILING_FRAMES * FRAME_MS, MIN_LOUD_SPEECH_FRAMES * FRAME_MS,
                TRAILING_REL_LEVEL, TRAILING_MIN_RMS, aggressiveness or 2)

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


def _frame_stats(pcm: bytes) -> tuple[list[bool], list[float]]:
    """Per 20 ms frame: webrtcvad flag and RMS. Trailing bytes shorter than a frame are ignored."""
    vad, rms = [], []
    for i in range(0, (len(pcm) // 640) * 640, 640):
        frame = pcm[i : i + 640]
        vad.append(_vad.is_speech(frame, 16000))
        fs = struct.unpack("<320h", frame)
        rms.append(math.sqrt(sum(x * x for x in fs) / 320))
    return vad, rms


def _analyze_frames(frame_vad: list[bool], frame_rms: list[float]) -> dict:
    """Shared end-of-speech decision over already-measured 20 ms frames."""
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
    loud_enough = max(TRAILING_MIN_RMS, TRAILING_REL_LEVEL * speech_level)
    trailing_frames = min(TRAILING_FRAMES, num_frames)
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
    trailing_silent = trailing_speech_ratio < 0.20 and loud_speech_frames >= MIN_LOUD_SPEECH_FRAMES

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

    check_voice_activity() used to re-decode the whole buffer (an ffmpeg spawn plus a
    pure-Python RMS pass over every sample) on every check. The cost grew with the
    utterance, which was most of the gap between the user stopping and the board
    answering. Here each packet is decoded exactly once and only the 20 ms frame
    stats are kept, so a check costs the same at second 8 as at second 1.
    """

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self.consumed = 0
        self.frame_vad: list[bool] = []
        self.frame_rms: list[float] = []
        self._tail = b""  # PCM left over from the last decode, shorter than one 20 ms frame

    def reset(self) -> None:
        self.consumed = 0
        self.frame_vad.clear()
        self.frame_rms.clear()
        self._tail = b""

    def feed(self, packets: list[bytes]) -> bool:
        """Decodes the packets added since the last call. False if decoding failed."""
        new = packets[self.consumed:]
        if not new:
            return True
        pcm = opus_packets_to_pcm(new, self.sample_rate)
        if not pcm:
            return False
        self.consumed = len(packets)
        buf = self._tail + pcm
        used = (len(buf) // 640) * 640
        self._tail = buf[used:]
        vad, rms = _frame_stats(buf[:used])
        self.frame_vad.extend(vad)
        self.frame_rms.extend(rms)
        return True

    def analyze(self) -> dict:
        return _analyze_frames(self.frame_vad, self.frame_rms)


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

    frame_vad, frame_rms = _frame_stats(pcm)
    return _analyze_frames(frame_vad, frame_rms)
