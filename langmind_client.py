import asyncio
import json
import logging
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("LangMindClient")

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


class LangMindError(RuntimeError):
    pass


class LangMindClient:
    def __init__(self, base_url: str, api_key: str, agent_id: str, voice_speed: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id
        self.voice_speed = voice_speed
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
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=120),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

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
                if res.status_code < 500 and res.status_code != 429:
                    raise LangMindError(f"{name} HTTP {res.status_code}")
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
        payload = {"agent_id": self.agent_id, "query": query,
                   "conversation_id": conversation_id, "attachment_ids": []}
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
        payload = {"agent_id": self.agent_id, "query": query,
                   "conversation_id": conversation_id, "attachment_ids": []}
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
                            if raw == "[DONE]":
                                break
                            try:
                                ev = json.loads(raw)
                            except ValueError:
                                continue
                            kind = ev.get("event")
                            if ev.get("conversation_id"):
                                yield ("conv", ev["conversation_id"])
                            if kind == "message" and ev.get("answer"):
                                got_text = True
                                yield ("text", ev["answer"])
                            elif kind in ("error",):
                                raise LangMindError("Chat stream provider error")
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

    async def _chat_stream(self, query: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """Fallback to SSE streaming /chat/message if blocking is unavailable."""
        url = f"{self.base_url}/chat/message"
        headers = {**self._headers(), "Accept": "text/event-stream"}
        payload = {"agent_id": self.agent_id, "query": query,
                   "conversation_id": conversation_id, "attachment_ids": []}
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
                    if "message" in ev:
                        chunks.append(ev["message"])
                    elif "text" in ev:
                        chunks.append(ev["text"])
        except httpx.HTTPError as exc:
            raise LangMindError("Chat stream network error") from exc
        return "".join(chunks), new_conv_id

    async def speech(self, text: str) -> bytes:
        """POST /v1/agents/{agent_id}/voice/speech. Returns b'' on failure (caller shows voice error)."""
        url = f"{self.base_url}/v1/agents/{self.agent_id}/voice/speech"
        payload = {"text": text, "speed": self.voice_speed, "format": "wav"}
        logger.info("Calling LangMind TTS")
        try:
            res = await self._post("TTS", url, json=payload)
            return res.content
        except LangMindError:
            logger.error("LangMind TTS request failed")
            return b""
