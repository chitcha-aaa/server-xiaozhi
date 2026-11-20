"""Board profiles, per-device authentication, and OTA token resolution.

Separates *hardware/firmware model* (`profiles`) from *physical board instances*
(`devices`) so 10+ heterogeneous boards can share a single server and LangMind
agent while keeping independent VAD, audio, and screen settings.
"""
from dataclasses import dataclass
import hmac
import logging
import os
from typing import Any, Mapping

from app.audio_converter import VadConfig
from app.speech_text import DEFAULT_STATUS_CATALOG, StatusCatalog

logger = logging.getLogger("Devices")

MIN_TOKEN_LEN = 32


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def _parse_size(val: Any, default: tuple[int, int]) -> tuple[int, int]:
    if isinstance(val, (list, tuple)) and len(val) == 2:
        try:
            w, h = int(val[0]), int(val[1])
            if w > 0 and h > 0:
                return (w, h)
        except (TypeError, ValueError):
            pass
    return default


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16000
    frame_duration_ms: int = 60
    tts_encode_frame_ms: int = 20
    tts_bitrate: str = "16k"
    max_speech_sec: float = 10.0
    vad_check_every_packets: int = 3
    vad_check_min_packets: int = 10

    @property
    def max_speech_packets(self) -> int:
        return max(1, int(self.max_speech_sec * 1000 / self.frame_duration_ms))

    @property
    def keep_on_clear_packets(self) -> int:
        return max(1, 1000 // self.frame_duration_ms)


@dataclass(frozen=True)
class ScreenConfig:
    status_channel: str = "both"  # "stt", "tts", or "both"
    screen_image: bool = False
    size: tuple[int, int] = (240, 240)
    mcp_enabled: bool = True


@dataclass(frozen=True)
class BoardProfile:
    name: str
    audio: AudioConfig = AudioConfig()
    vad: VadConfig = VadConfig()
    screen: ScreenConfig = ScreenConfig()
    status: StatusCatalog = DEFAULT_STATUS_CATALOG


@dataclass(frozen=True)
class DeviceContext:
    """Resolved identity and hardware profile for one connected board."""
    name: str
    device_id: str
    profile: BoardProfile


def _merge_audio(raw: Mapping[str, Any] | None, base: AudioConfig) -> AudioConfig:
    if not raw:
        return base
    return AudioConfig(
        sample_rate=int(raw.get("sample_rate", base.sample_rate)),
        frame_duration_ms=int(raw.get("frame_duration_ms", base.frame_duration_ms)),
        tts_encode_frame_ms=int(raw.get("tts_encode_frame_ms", base.tts_encode_frame_ms)),
        tts_bitrate=str(raw.get("tts_bitrate", base.tts_bitrate)),
        max_speech_sec=float(raw.get("max_speech_sec", base.max_speech_sec)),
        vad_check_every_packets=max(1, int(raw.get("vad_check_every_packets", base.vad_check_every_packets))),
        vad_check_min_packets=max(2, int(raw.get("vad_check_min_packets", base.vad_check_min_packets))),
    )


def _merge_vad(raw: Mapping[str, Any] | None, base: VadConfig) -> VadConfig:
    if not raw:
        return base
    return VadConfig(
        trailing_silence_ms=int(raw.get("trailing_silence_ms", base.trailing_silence_ms)),
        min_loud_speech_ms=int(raw.get("min_loud_speech_ms", base.min_loud_speech_ms)),
        trailing_rel_level=float(raw.get("trailing_rel_level", base.trailing_rel_level)),
        trailing_min_rms=float(raw.get("trailing_min_rms", base.trailing_min_rms)),
        aggressiveness=int(raw.get("aggressiveness", raw.get("vad_aggressiveness", base.aggressiveness))),
    )


def _merge_screen(raw: Mapping[str, Any] | None, base: ScreenConfig) -> ScreenConfig:
    if not raw:
        return base
    channel = str(raw.get("status_channel", base.status_channel)).strip().lower()
    if channel not in ("stt", "tts", "both"):
        channel = base.status_channel
    return ScreenConfig(
        status_channel=channel,
        screen_image=_as_bool(raw.get("screen_image"), base.screen_image),
        size=_parse_size(raw.get("size"), base.size),
        mcp_enabled=_as_bool(raw.get("mcp"), base.mcp_enabled),
    )


def _build_profile(name: str, raw: Mapping[str, Any], base: BoardProfile) -> BoardProfile:
    audio_raw = dict(raw.get("audio") or {})
    vad_raw = dict(raw.get("vad") or {})
    # Allow check_every_packets / check_min_packets / max_speech_sec in either audio or vad
    for src_key, dst_key in (
        ("check_every_packets", "vad_check_every_packets"),
        ("check_min_packets", "vad_check_min_packets"),
        ("max_speech_sec", "max_speech_sec"),
    ):
        if src_key in vad_raw and dst_key not in audio_raw:
            audio_raw[dst_key] = vad_raw[src_key]
    # Also allow legacy VAD fields placed inside audio:
    for vad_key in (
        "trailing_silence_ms",
        "min_loud_speech_ms",
        "trailing_rel_level",
        "trailing_min_rms",
        "vad_aggressiveness",
        "aggressiveness",
    ):
        if vad_key in audio_raw and vad_key not in vad_raw:
            vad_raw[vad_key] = audio_raw[vad_key]

    return BoardProfile(
        name=name,
        audio=_merge_audio(audio_raw, base.audio),
        vad=_merge_vad(vad_raw, base.vad),
        screen=_merge_screen(raw.get("screen"), base.screen),
        status=StatusCatalog.from_dict(raw.get("status_text"), base=base.status),
    )


class DeviceRegistry:
    """Resolves board tokens, MAC-to-device mappings, and per-model BoardProfiles."""

    def __init__(
        self,
        profiles: dict[str, BoardProfile],
        tokens: dict[str, str],
        mac_to_device: dict[str, str],
        device_profiles: dict[str, str],
        default_token: str = "",
    ):
        self._profiles = profiles
        self._tokens = tokens
        self._mac_to_device = mac_to_device
        self._device_profiles = device_profiles
        self._default_token = default_token

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "DeviceRegistry":
        cfg = config or {}
        environ = os.environ if env is None else env

        # 1. Base default profile built from top-level audio / server / status_text
        server_cfg = cfg.get("server") or {}
        audio_cfg = cfg.get("audio") or {}
        base_default = BoardProfile(
            name="default",
            audio=_merge_audio(audio_cfg, AudioConfig()),
            vad=_merge_vad(audio_cfg, VadConfig()),
            screen=_merge_screen(server_cfg, ScreenConfig()),
            status=StatusCatalog.from_dict(cfg.get("status_text"), base=DEFAULT_STATUS_CATALOG),
        )

        raw_profiles = cfg.get("profiles") or {}
        if isinstance(raw_profiles.get("default"), Mapping):
            base_default = _build_profile("default", raw_profiles["default"], base_default)

        profiles: dict[str, BoardProfile] = {"default": base_default}
        for pname, pdata in raw_profiles.items():
            pname_str = str(pname).strip()
            if not pname_str or pname_str == "default" or not isinstance(pdata, Mapping):
                continue
            profiles[pname_str] = _build_profile(pname_str, pdata, base_default)

        # 2. Load device tokens, MACs, and profile assignments
        tokens: dict[str, str] = {}
        mac_to_device: dict[str, str] = {}
        device_profiles: dict[str, str] = {}

        default_token = environ.get("DEVICE_TOKEN", "").strip()
        if default_token:
            tokens["board1"] = default_token

        # Env DEVICE_TOKENS="name:token,name:token"
        for item in environ.get("DEVICE_TOKENS", "").strip().split(","):
            name, _, value = item.partition(":")
            name, value = name.strip(), value.strip()
            if name and len(value) >= MIN_TOKEN_LEN:
                tokens[name] = value

        # Config devices: supports both legacy {name: token_str} and new {name: {mac, profile, token}}
        for raw_name, entry in (cfg.get("devices") or {}).items():
            name = str(raw_name).strip()
            if not name:
                continue
            if isinstance(entry, str):
                val = entry.strip()
                if len(val) >= MIN_TOKEN_LEN:
                    tokens[name] = val
            elif isinstance(entry, Mapping):
                val = str(entry.get("token", "") or "").strip()
                if len(val) >= MIN_TOKEN_LEN:
                    tokens[name] = val
                mac = str(entry.get("mac", "") or "").strip().lower()
                if mac:
                    mac_to_device[mac] = name
                prof = str(entry.get("profile", "") or "").strip()
                if prof:
                    if prof in profiles:
                        device_profiles[name] = prof
                    else:
                        logger.warning("Device %s references unknown profile %r; using default", name, prof)

        # Legacy config device_ids: {mac: name}
        for mac, name in (cfg.get("device_ids") or {}).items():
            mac_s, name_s = str(mac).strip().lower(), str(name).strip()
            if mac_s and name_s:
                mac_to_device[mac_s] = name_s

        # Env DEVICE_IDS="mac:name,mac:name" (overrides config)
        for item in environ.get("DEVICE_IDS", "").split(","):
            mac, _, name = item.rpartition(":")
            mac_s, name_s = mac.strip().lower(), name.strip()
            if mac_s and name_s:
                mac_to_device[mac_s] = name_s

        return cls(
            profiles=profiles,
            tokens=tokens,
            mac_to_device=mac_to_device,
            device_profiles=device_profiles,
            default_token=default_token,
        )

    @property
    def default_profile(self) -> BoardProfile:
        return self._profiles["default"]

    def get_profile(self, name: str) -> BoardProfile | None:
        return self._profiles.get(name)

    def has_tokens(self) -> bool:
        return bool(self._tokens)

    def _match_token(self, supplied: str) -> str | None:
        """Returns the device name for a valid token, or None. Constant-time across all tokens."""
        matched = None
        for name, value in self._tokens.items():
            if hmac.compare_digest(supplied, value):
                matched = name
        return matched

    def ota_token_for(self, device_id: str | None) -> str:
        """Returns the token /ota should hand to this board."""
        mac = (device_id or "").strip().lower()
        name = self._mac_to_device.get(mac)
        if name and name in self._tokens:
            return self._tokens[name]
        if self._default_token:
            return self._default_token
        if self._tokens:
            return next(iter(self._tokens.values()))
        return ""

    def authenticate(self, supplied_token: str, header_device_id: str | None) -> DeviceContext | None:
        """Authenticates a WebSocket token and resolves its DeviceContext & BoardProfile."""
        if not supplied_token:
            return None
        device_name = self._match_token(supplied_token)
        if not device_name:
            return None

        raw_id = (header_device_id or "").strip()
        mac_key = raw_id.lower()
        # Prefer profile mapped to the MAC (in case multiple boards share a fallback token),
        # otherwise use the profile mapped to the authenticated device_name.
        mac_owner = self._mac_to_device.get(mac_key)
        resolved_name = mac_owner or device_name
        profile_name = (
            self._device_profiles.get(resolved_name)
            or self._device_profiles.get(device_name)
            or "default"
        )
        profile = self._profiles.get(profile_name, self.default_profile)

        # Never fall back to a shared literal like "authenticated-device": use device_name
        # so two boards that omit the Device-Id header do not evict each other's sessions.
        device_id = raw_id or device_name
        return DeviceContext(name=resolved_name, device_id=device_id, profile=profile)
