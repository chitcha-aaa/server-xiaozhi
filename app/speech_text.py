"""Text helpers for spoken replies: strip non-speakable markup and split into TTS-sized chunks."""
from dataclasses import dataclass
import re

# Short English status strings shown on the device screen. Nothing else is ever sent to the screen.
STATUS_LISTENING = "Listening..."
STATUS_THINKING = "Thinking..."
STATUS_SPEAKING = "Speaking"
STATUS_NO_SPEECH = "Didn't catch that"
STATUS_ERROR_STT = "Error: speech API"
STATUS_ERROR_LLM = "Error: AI API"
STATUS_ERROR_TTS = "Error: voice API"
STATUS_READY = "Ready"
_STATUS_NAMES = ("listening", "thinking", "speaking", "no_speech",
                 "error_stt", "error_llm", "error_tts", "ready")


@dataclass(frozen=True)
class StatusCatalog:
    """Immutable allowlist of on-screen status strings (English defaults)."""
    listening: str = STATUS_LISTENING
    thinking: str = STATUS_THINKING
    speaking: str = STATUS_SPEAKING
    no_speech: str = STATUS_NO_SPEECH
    error_stt: str = STATUS_ERROR_STT
    error_llm: str = STATUS_ERROR_LLM
    error_tts: str = STATUS_ERROR_TTS
    ready: str = STATUS_READY

    @classmethod
    def from_dict(cls, texts: dict | None = None, base: "StatusCatalog | None" = None) -> "StatusCatalog":
        src = base or DEFAULT_STATUS_CATALOG
        if not texts:
            return src
        kwargs = {}
        for name in _STATUS_NAMES:
            val = texts.get(name)
            kwargs[name] = val.strip() if isinstance(val, str) and val.strip() else getattr(src, name)
        return cls(**kwargs)

    @property
    def allowed(self) -> frozenset[str]:
        return frozenset(getattr(self, n) for n in _STATUS_NAMES)

    def text_for_slot(self, slot: str) -> str | None:
        return getattr(self, slot, None) if slot in _STATUS_NAMES else None

    def slot_for_text(self, text: str) -> str | None:
        for name in _STATUS_NAMES:
            if getattr(self, name) == text:
                return name
        return None


DEFAULT_STATUS_CATALOG = StatusCatalog()


def _rebuild_allowed() -> None:
    global ALLOWED_STATUS
    ALLOWED_STATUS = {globals()[f"STATUS_{n.upper()}"] for n in _STATUS_NAMES}


ALLOWED_STATUS: set[str] = set()
_rebuild_allowed()


def configure(texts: dict) -> None:
    """Overrides the on-screen status strings from config (e.g. Thai wording).

    The device font decides what actually renders: a firmware built with a Chinese/Latin
    font shows missing glyphs as blank boxes, so any non-ASCII wording must be checked on
    the real screen before it is kept.
    """
    if not texts:
        return
    for name in _STATUS_NAMES:
        value = texts.get(name)
        if isinstance(value, str) and value.strip():
            globals()[f"STATUS_{name.upper()}"] = value.strip()
    _rebuild_allowed()

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]+"
)
_MD = re.compile(r"(\*\*|__|`+|^#+\s*|^\s*[-*•]\s+|^\s*\d+[.)]\s+)", re.M)
_URL = re.compile(r"https?://\S+")


def clean_for_speech(text: str) -> str:
    text = _URL.sub("", text)
    text = _EMOJI.sub("", text)
    text = _MD.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# Leading boilerplate the agent likes to open with. Removed only at the very start, and only
# if something substantive remains afterwards.
_GREETING = re.compile(
    r"^(?:\s*(?:สวัสดี(?:ครับ|ค่ะ|คะ)?|ยินดีต้อนรับ(?:ครับ|ค่ะ)|ยินดีให้บริการ(?:ครับ|ค่ะ))"
    r"[\s!.,]*(?:[\U0001F000-\U0001FAFF☀-➿️]+\s*)?)+"
)


def strip_greeting(text: str) -> str:
    rest = _GREETING.sub("", text, count=1).lstrip()
    return rest if len(rest) >= 8 else text


class StreamChunker:
    """Turns streamed LLM text deltas into TTS chunks as early as possible.

    The first chunk is cut at the first phrase break after FIRST_MIN chars (short text =
    fast TTS = fast first audio). Later chunks are longer so playback stays smooth.
    A leading greeting ("สวัสดีค่ะ ...") is dropped before anything is spoken.
    """
    FIRST_MIN, FIRST_MAX = 8, 28
    NEXT_MIN, NEXT_MAX = 50, 140
    _BREAK = re.compile(r"[\s,.!?;:、。！？\n]")

    def __init__(self):
        self.buf = ""
        self.emitted = 0
        self.greeting_checked = False

    def _cut(self, lo: int, hi: int) -> str | None:
        text = self.buf
        if len(text) < lo:
            return None
        # last break point in [lo, hi]; else first break after lo; else hard cut at hi
        window = text[:hi + 1]
        pos = max((m.end() for m in self._BREAK.finditer(window) if m.end() >= lo), default=None)
        if pos is None:
            m = self._BREAK.search(text, lo)
            if m:
                pos = m.end()
            elif len(text) > hi * 2:  # never cut a Thai word unless the run is very long
                pos = hi * 2
            else:
                return None
        chunk, self.buf = text[:pos], text[pos:]
        return chunk

    def feed(self, delta: str) -> list[str]:
        self.buf += delta
        if not self.greeting_checked:
            # Decide on a greeting only once a phrase break has arrived, so a partial
            # token like "สวัสดี" (before "ค่ะ") isn't cut in half.
            if not self._BREAK.search(self.buf.lstrip()):
                return []
            stripped = _GREETING.sub("", self.buf, count=1).lstrip()
            if stripped != self.buf.lstrip():
                if not stripped:
                    return []  # only greeting so far: wait for real content
                self.buf = stripped
            if len(self.buf) >= self.FIRST_MIN:
                self.greeting_checked = True
            else:
                return []
        out = []
        while True:
            lo, hi = (self.FIRST_MIN, self.FIRST_MAX) if self.emitted == 0 else (self.NEXT_MIN, self.NEXT_MAX)
            chunk = self._cut(lo, hi)
            if chunk is None:
                break
            chunk = clean_for_speech(chunk)
            if chunk:
                out.append(chunk)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        rest = clean_for_speech(self.buf)
        self.buf = ""
        if rest:
            self.emitted += 1
            return [rest]
        return []


def split_for_tts(text: str, max_len: int = 120) -> list[str]:
    """Split into speakable chunks. The first chunk is kept short so audio starts sooner.

    Thai has no sentence punctuation, so we split on newlines, Western punctuation and
    spaces (Thai uses a space between clauses), then pack pieces up to max_len.
    """
    text = clean_for_speech(text)
    if not text:
        return []
    pieces: list[str] = []
    for line in re.split(r"\n+", text):
        for part in re.split(r"(?<=[.!?。！？])\s+|\s+", line):
            part = part.strip()
            if part:
                pieces.append(part)

    chunks: list[str] = []
    cur = ""
    for p in pieces:
        limit = 60 if not chunks else max_len  # short first chunk -> faster first audio
        if cur and len(cur) + 1 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks
