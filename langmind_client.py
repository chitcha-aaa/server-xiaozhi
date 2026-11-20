import asyncio
import json
import logging
import socket
from typing import AsyncGenerator, Optional, Tuple
from urllib.parse import urlparse

import httpx

from speech_text import StreamChunker, clean_for_speech, split_for_tts

logger = logging.getLogger("LangMindClient")


def clean_text_for_tts(text: str) -> str:
    """Compatibility helper from feat/low-latency-streaming; delegates to speech_text.clean_for_speech."""
    return clean_for_speech(text)


def split_text_into_chunks(text: str, first_chunk_len: int = 25, next_chunk_len: int = 70) -> list[str]:
    """Compatibility helper from feat/low-latency-streaming; splits cleaned text into TTS chunks."""
    cleaned = clean_for_speech(text)
    if not cleaned:
        return []
    if len(cleaned) <= first_chunk_len + 15:
        return [cleaned]
    return split_for_tts(cleaned, max_len=max(next_chunk_len, first_chunk_len))

# ---------------------------------------------------------------------------
# Resilience against a flaky uplink (phone hotspot): DNS lookups for the API
# host intermittently fail with getaddrinfo errors even when the route works.
# Remember the last good answer per host and fall back to it on failure.
# ---------------------------------------------------------------------------
_dns_cache: dict = {}
_orig_getaddrinfo = socket.getaddrinfo
_cached_hosts: set = set()


def _getaddrinfo(host, *args, **kwargs):
    # anyio/httpx pass the host IDNA-encoded as bytes; normalise for the cache key.
    name = host.decode("ascii", "ignore") if isinstance(host, (bytes, bytearray)) else host
    key = (name, args, tuple(sorted(kwargs.items())))
    try:
        res = _orig_getaddrinfo(host, *args, **kwargs)
        if name in _cached_hosts:
            _dns_cache[key] = res
        return res
    except socket.gaierror:
        cached = _dns_cache.get(key) if name in _cached_hosts else None
        if cached:
            logger.warning("DNS lookup failed; using cached address")
            return cached
        raise


socket.getaddrinfo = _getaddrinfo

RETRY_DELAYS = (0.4, 1.2)  # 3 attempts total
MAX_RETRY_AFTER_SEC = 10.0


def _retry_delay(attempt: int, retry_after: Optional[str] = None) -> float:
    delay = RETRY_DELAYS[attempt]
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SEC))
        except ValueError:
            pass
    return delay


def _format_error_detail(res: httpx.Response) -> str:
    """Format rate-limit/proxy headers and truncated body for non-200 diagnostics."""
    interesting = {
        k: v for k, v in res.headers.items()
        if k.lower() in ("retry-after", "server", "cf-ray") or "ratelimit" in k.lower()
    }
    body = res.text[:300].replace("\n", " ").strip()
    return f"headers={interesting} body={body!r}"


class LangMindError(RuntimeError):
    pass


class LangMindClient:
    def __init__(self, base_url: str, api_key: str, agent_id: str, voice_speed: float = 1.0,
                 input_mode: Optional[str] = "voice"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id
        self.voice_speed = voice_speed
        self.input_mode = (input_mode or "").strip()
        host = urlparse(self.base_url).hostname
        if host:
            _cached_hosts.add(host)
        # One shared client: keeps TLS connections alive between turns, so most
        # requests need no new DNS lookup or handshake at all.
        self._client: Optional[httpx.AsyncClient] = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=8.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=120),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def close(self) -> None:
        await self.aclose()

    def _chat_payload(self, query: str, conversation_id: Optional[str]) -> dict:
        payload = {
            "agent_id": self.agent_id,
            "query": query,
            "conversation_id": conversation_id,
            "attachment_ids": [],
        }
        if self.input_mode:
            payload["input_mode"] = self.input_mode
        return payload

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "XiaoZhi-LangMind-Bridge/1.0",
        }

    async def _post(self, name: str, url: str, **kwargs) -> httpx.Response:
        """POST with retries on network errors, 429, and 5xx. Raises LangMindError on final failure."""
        extra_headers = kwargs.pop("extra_headers", {})
        last = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            retry_after = None
            try:
                res = await self._http().post(url, headers=self._headers() | extra_headers, **kwargs)
            except httpx.HTTPError:
                last = "network error"
            else:
                if res.status_code == 200:
                    return res
                if attempt == 0 or res.status_code != 429:
                    logger.warning(f"LangMind {name} HTTP {res.status_code} {_format_error_detail(res)}")
                if res.status_code < 500 and res.status_code != 429:
                    raise LangMindError(f"{name} HTTP {res.status_code}")
                if res.status_code == 429 and "QUOTA_EXCEEDED" in res.text:
                    raise LangMindError(f"{name} HTTP 429 (quota exceeded)")
                last = f"HTTP {res.status_code}"
                retry_after = res.headers.get("Retry-After")
            if attempt < len(RETRY_DELAYS):
                delay = _retry_delay(attempt, retry_after)
                logger.warning(f"LangMind {name} {last}; retry {attempt + 1}/{len(RETRY_DELAYS)} in {delay:.1f}s")
                await asyncio.sleep(delay)
        raise LangMindError(f"{name} {last}")

    async def transcribe(self, wav_bytes: bytes) -> str:
        """POST /v1/agents/{agent_id}/voice/transcribe. Raises on API failure."""
        url = f"{self.base_url}/v1/agents/{self.agent_id}/voice/transcribe"
        logger.info("Calling LangMind STT")
        res = await self._post("STT", url, files={"audio": ("audio.wav", wav_bytes, "audio/wav")})
        data = res.json()
        if isinstance(data, dict):
            if "text" in data:
                return data["text"]
            if isinstance(data.get("data"), dict) and "text" in data["data"]:
                return data["data"]["text"]
        return ""

    async def chat(self, query: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """POST /chat/message/blocking. Raises on API failure (never returns an error text to speak)."""
        url = f"{self.base_url}/chat/message/blocking"
        payload = self._chat_payload(query, conversation_id)
        logger.info("Calling LangMind Chat")
        try:
            res = await self._post("Chat", url, json=payload)
        except LangMindError as e:
            if "HTTP 404" in str(e):
                logger.warning("Blocking endpoint not found, falling back to /chat/message stream")
                return await self._chat_stream(query, conversation_id)
            raise
        data = res.json()
        answer, new_conv_id = "", conversation_id
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            answer = inner.get("answer") or inner.get("message") or inner.get("content") or inner.get("text") or ""
            new_conv_id = inner.get("conversation_id") or data.get("conversation_id") or conversation_id
        return answer, new_conv_id

    async def chat_stream(self, query: str, conversation_id: Optional[str] = None):
        """POST /chat/message (SSE). Async generator yielding ("conv", id) once and ("text", delta)
        as tokens arrive, so TTS can start on the first sentence. Raises LangMindError on failure
        before any text; retries the connection like _post."""
        url = f"{self.base_url}/chat/message"
        headers = {**self._headers(), "Accept": "text/event-stream"}
        payload = self._chat_payload(query, conversation_id)
        logger.info("Calling LangMind Chat stream")
        last = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            got_text = False
            retry_after = None
            try:
                async with self._http().stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 500 or resp.status_code == 429:
                        last = f"HTTP {resp.status_code}"
                        retry_after = resp.headers.get("Retry-After")
                    elif resp.status_code != 200:
                        raise LangMindError(f"Chat HTTP {resp.status_code}")
                    else:
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if not raw or raw == "[DONE]":
                                break
                            try:
                                ev = json.loads(raw)
                            except ValueError:
                                continue
                            if not isinstance(ev, dict):
                                continue
                            kind = ev.get("event")
                            if ev.get("conversation_id"):
                                yield ("conv", ev["conversation_id"])
                            if kind in ("message_end", "done"):
                                break
                            if kind == "error":
                                raise LangMindError("Chat stream provider error")
                            if kind in ("message", "delta", "", None) or "answer" in ev:
                                token = (
                                    ev.get("answer")
                                    or ev.get("message")
                                    or ev.get("text")
                                    or ev.get("content")
                                    or ""
                                )
                                if isinstance(token, str) and token:
                                    got_text = True
                                    yield ("text", token)
                        return
            except httpx.HTTPError:
                if got_text:  # mid-answer drop: keep what was spoken, stop here
                    logger.warning("Chat stream dropped mid-answer")
                    return
                last = "network error"
            if attempt < len(RETRY_DELAYS):
                delay = _retry_delay(attempt, retry_after)
                logger.warning(f"LangMind Chat stream {last}; retry {attempt + 1}/{len(RETRY_DELAYS)} in {delay:.1f}s")
                await asyncio.sleep(delay)
        raise LangMindError(f"Chat stream {last}")

    async def stream_chat_chunks(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        first_chunk_min_len: int = 8,
        next_chunk_min_len: int = 50,
    ) -> AsyncGenerator[Tuple[str, Optional[str]], None]:
        """Streams SSE chat tokens and yields cleaned phrase chunks as (chunk_text, conversation_id)."""
        chunker = StreamChunker()
        chunker.FIRST_MIN = first_chunk_min_len
        chunker.FIRST_MAX = max(chunker.FIRST_MAX, first_chunk_min_len + 20)
        chunker.NEXT_MIN = next_chunk_min_len
        chunker.NEXT_MAX = max(chunker.NEXT_MAX, next_chunk_min_len + 60)
        new_conv_id = conversation_id
        async for kind, value in self.chat_stream(query, conversation_id):
            if kind == "conv":
                new_conv_id = value
                continue
            for chunk in chunker.feed(value):
                yield chunk, new_conv_id
        for chunk in chunker.flush():
            yield chunk, new_conv_id

    async def _chat_stream(self, query: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """Fallback to SSE streaming /chat/message if blocking is unavailable."""
        url = f"{self.base_url}/chat/message"
        headers = {**self._headers(), "Accept": "text/event-stream"}
        payload = self._chat_payload(query, conversation_id)
        chunks, new_conv_id = [], conversation_id
        try:
            async with self._http().stream("POST", url, headers=headers, json=payload) as response:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    text = line[5:].strip()
                    if text == "[DONE]":
                        break
                    try:
                        ev = json.loads(text)
                    except ValueError:
                        continue
                    new_conv_id = ev.get("conversation_id", new_conv_id)
                    if "answer" in ev:
                        chunks.append(ev["answer"])
                    elif "message" in ev:
                        chunks.append(ev["message"])
                    elif "text" in ev:
                        chunks.append(ev["text"])
        except httpx.HTTPError as exc:
            raise LangMindError("Chat stream network error") from exc
        return "".join(chunks), new_conv_id

    async def speech(self, text: str) -> bytes:
        """POST /v1/agents/{agent_id}/voice/speech. Returns b'' on failure (caller shows voice error)."""
        cleaned = clean_for_speech(text)
        if not cleaned:
            return b""
        url = f"{self.base_url}/v1/agents/{self.agent_id}/voice/speech"
        payload = {"text": cleaned, "speed": self.voice_speed, "format": "wav"}
        logger.info("Calling LangMind TTS")
        try:
            res = await self._post("TTS", url, json=payload)
            return res.content
        except LangMindError:
            logger.error("LangMind TTS request failed")
            return b""
