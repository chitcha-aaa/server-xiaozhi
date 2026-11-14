import httpx
import logging
from typing import Tuple, Optional

logger = logging.getLogger("LangMindClient")

class LangMindClient:
    def __init__(self, base_url: str, api_key: str, agent_id: str, voice_speed: float = 1.0):
        # Normalize base URL (strip trailing slash)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.agent_id = agent_id
        self.voice_speed = voice_speed

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "XiaoZhi-LangMind-Bridge/1.0",
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

        # Raises on API failure so the caller can show "Error: speech API" instead of
        # pretending the user said nothing. One retry on 5xx / network errors.
        async with httpx.AsyncClient(timeout=15.0) as client:
            for attempt in (1, 2):
                logger.info(f"Calling LangMind STT: {url} ({len(wav_bytes)} bytes)")
                try:
                    res = await client.post(url, headers=headers, files=files)
                except httpx.HTTPError as e:
                    if attempt == 1:
                        logger.warning(f"LangMind STT network error, retrying: {e}")
                        continue
                    raise RuntimeError(f"STT network error: {e}") from e
                if res.status_code >= 500 and attempt == 1:
                    logger.warning(f"LangMind STT [{res.status_code}], retrying once")
                    continue
                if res.status_code != 200:
                    raise RuntimeError(f"STT HTTP {res.status_code}: {res.text[:200]}")
                break

            data = res.json()
            # Expected format: {"text": "...", "elapsed_ms": ...} or {"data": {"text": "..."}}
            if isinstance(data, dict):
                if "text" in data:
                    return data["text"]
                if "data" in data and isinstance(data["data"], dict) and "text" in data["data"]:
                    return data["data"]["text"]
            return ""

    async def chat(self, query: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """
        Sends user text to LangMind Agent:
        POST /chat/message/blocking
        """
        url = f"{self.base_url}/chat/message/blocking"
        headers = {
            **self._headers(),
            "Content-Type": "application/json"
        }
        payload = {
            "agent_id": self.agent_id,
            "query": query,
            "conversation_id": conversation_id,
            "attachment_ids": []
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"Calling LangMind Chat: {url} ({len(query)} chars)")
                res = await client.post(url, headers=headers, json=payload)
                
                # If /chat/message/blocking is not implemented, try /chat/message
                if res.status_code == 404:
                    logger.warning("Blocking endpoint not found, falling back to /chat/message stream")
                    return await self._chat_stream(query, conversation_id)

                if res.status_code != 200:
                    logger.error(f"LangMind Chat Error [{res.status_code}]: {res.text}")
                    return f"ขออภัย เกิดข้อผิดพลาดจาก LangMind (รหัส {res.status_code})", conversation_id

                data = res.json()
                logger.debug(f"LangMind Chat response: {data}")

                # Extract answer text and conversation_id
                answer = ""
                new_conv_id = conversation_id

                if isinstance(data, dict):
                    # Check standard Promout/LangMind envelope
                    inner = data.get("data") if isinstance(data.get("data"), dict) else data
                    answer = inner.get("answer") or inner.get("message") or inner.get("content") or inner.get("text") or ""
                    new_conv_id = inner.get("conversation_id") or data.get("conversation_id") or conversation_id

                return answer, new_conv_id
        except Exception as e:
            logger.error(f"Exception during LangMind chat: {e}")
            return "ขออภัย ไม่สามารถเชื่อมต่อกับ LangMind ได้ในขณะนี้", conversation_id

    async def _chat_stream(self, query: str, conversation_id: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """Fallback to SSE streaming /chat/message if blocking is unavailable."""
        url = f"{self.base_url}/chat/message"
        headers = {
            **self._headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream"
        }
        payload = {
            "agent_id": self.agent_id,
            "query": query,
            "conversation_id": conversation_id,
            "attachment_ids": []
        }

        answer_chunks = []
        new_conv_id = conversation_id

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        event_text = line[5:].strip()
                        if event_text == "[DONE]":
                            break
                        try:
                            import json
                            event_data = json.loads(event_text)
                            if "conversation_id" in event_data:
                                new_conv_id = event_data["conversation_id"]
                            # Append chunk
                            if "message" in event_data:
                                answer_chunks.append(event_data["message"])
                            elif "text" in event_data:
                                answer_chunks.append(event_data["text"])
                        except Exception:
                            pass
            return "".join(answer_chunks), new_conv_id
        except Exception as e:
            logger.error(f"Exception in _chat_stream: {e}")
            return "ขออภัย เกิดข้อผิดพลาดในการรับข้อมูล", conversation_id

    async def speech(self, text: str) -> bytes:
        """
        Sends text to LangMind TTS:
        POST /v1/agents/{agent_id}/voice/speech
        """
        url = f"{self.base_url}/v1/agents/{self.agent_id}/voice/speech"
        headers = {
            **self._headers(),
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "speed": self.voice_speed,
            "format": "wav"
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                logger.info(f"Calling LangMind TTS: {url} ({len(text)} chars)")
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code != 200:
                    logger.error(f"LangMind TTS Error [{res.status_code}]: {res.text}")
                    return b""
                return res.content
        except Exception as e:
            logger.error(f"Exception during LangMind speech: {e}")
            return b""
