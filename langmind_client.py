import json
import logging
import re
from typing import AsyncGenerator, Optional, Tuple
import httpx

logger = logging.getLogger("LangMindClient")


def clean_text_for_tts(text: str) -> str:
    """Removes Markdown formatting and excess symbols so TTS reads smoothly."""
    if not text:
        return ""
    # Replace markdown links [label](url) with label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove markdown bold/italic/code/header symbols
    text = re.sub(r"[*_`#~>]+", "", text)
    # Replace bullet dashes at line start with space
    text = re.sub(r"(?m)^\s*[-•]\s*", " ", text)
    # Collapse multiple spaces/newlines into single space
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_HANGING_THAI_WORDS = (
    "ของ", "ที่", "ใน", "จาก", "กับ", "และ", "หรือ", "เพื่อ",
    "โดย", "ซึ่ง", "แต่", "ถึง", "ให้", "ว่า", "คือ", "เป็น", "แบบ", "ทาง",
)


def _is_good_clause_break(text_before_break: str) -> bool:
    """Returns False if the chunk ends with a hanging Thai preposition/conjunction."""
    stripped = text_before_break.strip()
    if not stripped:
        return False
    for w in _HANGING_THAI_WORDS:
        if stripped.endswith(w):
            return False
    return True


def split_text_into_chunks(text: str, first_chunk_len: int = 25, next_chunk_len: int = 70) -> list[str]:
    """
    Splits full text into natural speaking chunks for Thai/English:
    - First chunk is shorter (~25-40 chars) for minimum time-to-first-audio.
    - Subsequent chunks are longer (~70-100 chars) for natural prosody and fewer HTTP calls.
    """
    cleaned = clean_text_for_tts(text)
    if not cleaned:
        return []
    if len(cleaned) <= first_chunk_len + 15:
        return [cleaned]

    tokens = re.split(r"(\s+|[.!?]+)", cleaned)
    chunks: list[str] = []
    current = ""

    for token in tokens:
        if not token:
            continue
        current += token
        target_len = first_chunk_len if len(chunks) == 0 else next_chunk_len

        if len(current.strip()) >= target_len and re.search(r"(\s+|[.!?]+)$", token) and _is_good_clause_break(current):
            chunks.append(current.strip())
            current = ""
        elif len(current.strip()) >= target_len * 2:
            # Force split if a very long segment has no spaces
            chunks.append(current.strip())
            current = ""

    remainder = current.strip()
    if remainder:
        if chunks and len(remainder) < 12:
            chunks[-1] = f"{chunks[-1]} {remainder}"
        else:
            chunks.append(remainder)

    return chunks


class LangMindClient:
    def __init__(self, base_url: str, api_key: str, agent_id: str, voice_speed: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id
        self.voice_speed = voice_speed
        # Persistent HTTP client with keep-alive pool to eliminate repeated TCP/TLS handshakes
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=60.0),
        )

    async def close(self):
        await self.client.aclose()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "XiaoZhi-LangMind-Bridge/2.0",
        }

    async def transcribe(self, wav_bytes: bytes) -> str:
        """
        Sends audio to LangMind STT:
        POST /v1/agents/{agent_id}/voice/transcribe
        """
        url = f"{self.base_url}/v1/agents/{self.agent_id}/voice/transcribe"
        headers = self._headers()
        files = {
            "audio": ("audio.wav", wav_bytes, "audio/wav")
        }

        try:
            logger.info(f"Calling LangMind STT: {url} ({len(wav_bytes)} bytes)")
            res = await self.client.post(url, headers=headers, files=files, timeout=15.0)
            if res.status_code != 200:
                logger.error(f"LangMind STT Error [{res.status_code}]: {res.text}")
                return ""

            data = res.json()
            if isinstance(data, dict):
                if "text" in data:
                    return data["text"]
                if "data" in data and isinstance(data["data"], dict) and "text" in data["data"]:
                    return data["data"]["text"]
            return ""
        except Exception as e:
            logger.error(f"Exception during LangMind transcribe: {e}")
            return ""

    async def stream_chat_chunks(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        first_chunk_min_len: int = 24,
        next_chunk_min_len: int = 65,
    ) -> AsyncGenerator[Tuple[str, Optional[str]], None]:
        """
        Streams LLM response via SSE (`POST /chat/message`) and yields sentence chunks
        as soon as natural phrase boundaries are reached, without waiting for full completion.
        Yields tuples of (chunk_text, updated_conversation_id).
        """
        url = f"{self.base_url}/chat/message"
        headers = {
            **self._headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "agent_id": self.agent_id,
            "query": query,
            "conversation_id": conversation_id,
            "input_mode": "voice",
            "attachment_ids": [],
        }

        new_conv_id = conversation_id
        buffer = ""
        chunk_count = 0
        yielded_any = False

        try:
            logger.info(f"Streaming LangMind Chat (SSE): {url} | query: {query}")
            async with self.client.stream("POST", url, headers=headers, json=payload, timeout=45.0) as response:
                if response.status_code != 200:
                    logger.warning(f"SSE Chat returned {response.status_code}, falling back to blocking chat")
                    answer_text, new_conv_id = await self.chat(query, conversation_id)
                    for chunk in split_text_into_chunks(answer_text, first_chunk_min_len, next_chunk_min_len):
                        yield chunk, new_conv_id
                    return

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    event_text = line[5:].strip()
                    if not event_text or event_text == "[DONE]":
                        break

                    try:
                        event_data = json.loads(event_text)
                    except Exception:
                        continue

                    if not isinstance(event_data, dict):
                        continue

                    if "conversation_id" in event_data and event_data["conversation_id"]:
                        new_conv_id = event_data["conversation_id"]

                    event_type = event_data.get("event", "")
                    if event_type in ("message_end", "done", "error"):
                        if event_type == "error":
                            logger.error(f"LangMind SSE error event: {event_data}")
                        break

                    # Extract token chunk from SSE payload (LangMind uses 'answer' on 'message' events)
                    token = ""
                    if event_type in ("message", " delta", "") or "answer" in event_data:
                        token = (
                            event_data.get("answer")
                            or event_data.get("message")
                            or event_data.get("text")
                            or event_data.get("content")
                            or ""
                        )
                        if not isinstance(token, str):
                            token = ""

                    if not token:
                        continue

                    buffer += token
                    target_min = first_chunk_min_len if chunk_count == 0 else next_chunk_min_len

                    # Check if we can cut a clean phrase chunk from buffer
                    if len(buffer.strip()) >= target_min:
                        # Look for natural break at the end or near the end of buffer
                        split_idx = -1
                        # Priority 1: buffer just ended with whitespace, newline, or sentence punctuation
                        if buffer.endswith((" ", "\n", ".", "!", "?")) and _is_good_clause_break(buffer):
                            split_idx = len(buffer)
                        # Priority 2: token just finished a polite Thai clause ending
                        elif token.endswith(("ครับ", "ค่ะ", "นะคะ", "นะครับ")) and len(buffer.strip()) >= target_min:
                            split_idx = len(buffer)
                        # Priority 3: buffer grew past 1.4x target_min, cut at last good space/newline
                        elif len(buffer.strip()) >= int(target_min * 1.4):
                            for sep in ("\n", ". ", "! ", "? ", " "):
                                pos = buffer.rfind(sep)
                                if pos >= target_min // 2 and _is_good_clause_break(buffer[:pos]):
                                    split_idx = pos + len(sep)
                                    break
                        # Priority 4: very long unspaced Thai stream (> 2.2x target_min), cut at token boundary
                        if split_idx == -1 and len(buffer.strip()) >= int(target_min * 2.2) and _is_good_clause_break(buffer):
                            split_idx = len(buffer)

                        if split_idx > 0:
                            raw_chunk = buffer[:split_idx]
                            buffer = buffer[split_idx:]
                            cleaned_chunk = clean_text_for_tts(raw_chunk)
                            if cleaned_chunk:
                                chunk_count += 1
                                yielded_any = True
                                yield cleaned_chunk, new_conv_id

            # Flush remaining buffer after stream completes
            if buffer.strip():
                cleaned_tail = clean_text_for_tts(buffer)
                if cleaned_tail:
                    yielded_any = True
                    yield cleaned_tail, new_conv_id

        except Exception as e:
            logger.error(f"Exception in stream_chat_chunks: {e}")
            if not yielded_any:
                yield "ขออภัย ไม่สามารถเชื่อมต่อกับ LangMind ได้ในขณะนี้", new_conv_id

    async def chat(self, query: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """
        Sends user text to LangMind Agent (full response, using streaming under the hood or blocking):
        """
        chunks = []
        new_conv_id = conversation_id
        async for chunk, conv_id in self.stream_chat_chunks(
            query, conversation_id, first_chunk_min_len=80, next_chunk_min_len=120
        ):
            chunks.append(chunk)
            if conv_id:
                new_conv_id = conv_id
        return " ".join(chunks).strip(), new_conv_id

    async def speech(self, text: str) -> bytes:
        """
        Sends text to LangMind TTS:
        POST /v1/agents/{agent_id}/voice/speech
        """
        cleaned = clean_text_for_tts(text)
        if not cleaned:
            return b""

        url = f"{self.base_url}/v1/agents/{self.agent_id}/voice/speech"
        headers = {
            **self._headers(),
            "Content-Type": "application/json",
        }
        payload = {
            "text": cleaned,
            "speed": self.voice_speed,
            "format": "wav",
        }

        try:
            logger.info(f"Calling LangMind TTS: {url} ({len(cleaned)} chars): '{cleaned}'")
            res = await self.client.post(url, headers=headers, json=payload, timeout=20.0)
            if res.status_code != 200:
                logger.error(f"LangMind TTS Error [{res.status_code}]: {res.text}")
                return b""
            return res.content
        except Exception as e:
            logger.error(f"Exception during LangMind speech: {e}")
            return b""
