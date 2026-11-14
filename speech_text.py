"""Text helpers for spoken replies: strip non-speakable markup and split into TTS-sized chunks."""
import re

# Short English status strings shown on the device screen (sent through the "stt" text channel,
# which firmware v1.4.x renders as a chat line). Nothing else is ever sent to the screen.
STATUS_LISTENING = "Listening..."
STATUS_THINKING = "Thinking..."
STATUS_SPEAKING = "Speaking"
STATUS_NO_SPEECH = "Didn't catch that"
STATUS_ERROR_STT = "Error: speech API"
STATUS_ERROR_LLM = "Error: AI API"
STATUS_ERROR_TTS = "Error: voice API"
STATUS_READY = "Ready"
ALLOWED_STATUS = {
    STATUS_LISTENING, STATUS_THINKING, STATUS_SPEAKING, STATUS_NO_SPEECH,
    STATUS_ERROR_STT, STATUS_ERROR_LLM, STATUS_ERROR_TTS, STATUS_READY,
}

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
