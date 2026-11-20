"""PostgreSQL-backed store for board profiles and devices.

Uses psycopg3 ConnectionPool wrapped in asyncio.to_thread so it works reliably
on Windows ProactorEventLoop, Linux, macOS, and FastAPI TestClient without
blocking the event loop.
"""
import asyncio
import datetime
import hmac
import logging
import re
import secrets
from typing import Any, Mapping

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.audio_converter import VadConfig
from app.devices import (
    AudioConfig,
    BoardProfile,
    DeviceContext,
    DeviceRegistry,
    ScreenConfig,
    _merge_audio,
    _merge_screen,
    _merge_vad,
)
from app.speech_text import DEFAULT_STATUS_CATALOG

logger = logging.getLogger("DeviceStore")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY,
    audio JSONB NOT NULL,
    vad JSONB NOT NULL,
    screen JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    profile_name TEXT NOT NULL REFERENCES profiles(name) ON UPDATE CASCADE,
    token TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'revoked')),
    firmware_version TEXT NOT NULL DEFAULT '',
    board_model TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_devices_status ON devices(status);
"""

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def profile_to_dicts(profile: BoardProfile) -> tuple[dict, dict, dict]:
    audio = {
        "sample_rate": profile.audio.sample_rate,
        "frame_duration_ms": profile.audio.frame_duration_ms,
        "tts_encode_frame_ms": profile.audio.tts_encode_frame_ms,
        "tts_bitrate": profile.audio.tts_bitrate,
        "max_speech_sec": profile.audio.max_speech_sec,
        "vad_check_every_packets": profile.audio.vad_check_every_packets,
        "vad_check_min_packets": profile.audio.vad_check_min_packets,
    }
    vad = {
        "trailing_silence_ms": profile.vad.trailing_silence_ms,
        "min_loud_speech_ms": profile.vad.min_loud_speech_ms,
        "trailing_rel_level": profile.vad.trailing_rel_level,
        "trailing_min_rms": profile.vad.trailing_min_rms,
        "aggressiveness": profile.vad.aggressiveness,
    }
    screen = {
        "status_channel": profile.screen.status_channel,
        "screen_image": profile.screen.screen_image,
        "size": [profile.screen.size[0], profile.screen.size[1]],
        "mcp": profile.screen.mcp_enabled,
    }
    return audio, vad, screen


def row_to_profile(row: Mapping[str, Any]) -> BoardProfile:
    return BoardProfile(
        name=row["name"],
        audio=_merge_audio(row.get("audio"), AudioConfig()),
        vad=_merge_vad(row.get("vad"), VadConfig()),
        screen=_merge_screen(row.get("screen"), ScreenConfig()),
        status=DEFAULT_STATUS_CATALOG,
    )


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime.datetime):
        return dt.isoformat()
    return str(dt)


def _mask_token(tok: str) -> str:
    if len(tok) <= 8:
        return "****"
    return f"{tok[:4]}...{tok[-4:]}"


def _serialize_device(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "device_id": row["device_id"],
        "name": row["name"],
        "profile_name": row["profile_name"],
        "token_preview": _mask_token(row["token"]),
        "status": row["status"],
        "firmware_version": row.get("firmware_version") or "",
        "board_model": row.get("board_model") or "",
        "created_at": _iso(row.get("created_at")),
        "approved_at": _iso(row.get("approved_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
    }


def _serialize_profile(row: Mapping[str, Any]) -> dict[str, Any]:
    prof = row_to_profile(row)
    audio, vad, screen = profile_to_dicts(prof)
    return {
        "name": prof.name,
        "audio": audio,
        "vad": vad,
        "screen": screen,
        "updated_at": _iso(row.get("updated_at")),
        "device_count": int(row.get("device_count", 0)),
    }


def _default_seed_profiles() -> list[BoardProfile]:
    return [
        BoardProfile(
            name="default",
            audio=AudioConfig(sample_rate=16000, frame_duration_ms=60, tts_encode_frame_ms=20, tts_bitrate="16k"),
            vad=VadConfig(
                trailing_silence_ms=800,
                min_loud_speech_ms=600,
                trailing_rel_level=0.15,
                trailing_min_rms=150.0,
                aggressiveness=2,
            ),
            screen=ScreenConfig(status_channel="stt", screen_image=False, size=(240, 240), mcp_enabled=False),
            status=DEFAULT_STATUS_CATALOG,
        ),
        BoardProfile(
            name="lynai",
            audio=AudioConfig(sample_rate=16000, frame_duration_ms=60, tts_encode_frame_ms=20, tts_bitrate="16k"),
            vad=VadConfig(
                trailing_silence_ms=500,
                min_loud_speech_ms=240,
                trailing_rel_level=0.25,
                trailing_min_rms=150.0,
                aggressiveness=3,
            ),
            screen=ScreenConfig(status_channel="tts", screen_image=True, size=(240, 240), mcp_enabled=True),
            status=DEFAULT_STATUS_CATALOG,
        ),
    ]


def _default_device_name(device_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9]", "", device_id).lower()
    suffix = clean[-4:] if len(clean) >= 4 else (clean or secrets.token_hex(2))
    return f"board-{suffix}"


class PostgresDeviceStore:
    """PostgreSQL store for board profiles, auto-registered devices, and approval state."""

    def __init__(self, database_url: str, fallback_registry: DeviceRegistry | None = None):
        self.database_url = database_url
        self.fallback_registry = fallback_registry
        self._pool: ConnectionPool | None = None

    def _open_and_init_sync(self, extra_profiles: list[BoardProfile] | None = None) -> None:
        self._pool = ConnectionPool(
            conninfo=self.database_url,
            min_size=1,
            max_size=10,
            open=True,
            kwargs={"row_factory": dict_row, "autocommit": False},
        )
        with self._pool.connection() as conn:
            conn.execute(_SCHEMA_SQL)
            # Seed built-in profiles first, then any profiles from config.yaml
            seeds = {p.name: p for p in _default_seed_profiles()}
            for p in extra_profiles or []:
                seeds[p.name] = p
            for prof in seeds.values():
                audio, vad, screen = profile_to_dicts(prof)
                conn.execute(
                    """
                    INSERT INTO profiles (name, audio, vad, screen)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    (prof.name, Jsonb(audio), Jsonb(vad), Jsonb(screen)),
                )
            conn.commit()
        logger.info("PostgreSQL device store initialized")

    async def init(self, extra_profiles: list[BoardProfile] | None = None) -> None:
        await asyncio.to_thread(self._open_and_init_sync, extra_profiles)

    def _close_sync(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    async def aclose(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _require_pool(self) -> ConnectionPool:
        if self._pool is None:
            raise RuntimeError("PostgresDeviceStore is not initialized")
        return self._pool

    # --- Board-facing operations -------------------------------------------------

    def _ota_register_or_get_sync(
        self,
        device_id: str,
        firmware_version: str = "",
        board_model: str = "",
    ) -> dict[str, Any]:
        pool = self._require_pool()
        norm_id = device_id.strip().lower()
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE device_id = %s",
                (norm_id,),
            ).fetchone()
            if row is not None:
                conn.execute(
                    """
                    UPDATE devices
                    SET last_seen_at = NOW(),
                        firmware_version = CASE WHEN %s <> '' THEN %s ELSE firmware_version END,
                        board_model = CASE WHEN %s <> '' THEN %s ELSE board_model END
                    WHERE device_id = %s
                    """,
                    (firmware_version, firmware_version, board_model, board_model, norm_id),
                )
                conn.commit()
                return dict(row)

            # New board -> auto-register as 'pending' with its own 32-hex token
            token = secrets.token_hex(16)
            name = _default_device_name(norm_id)
            profile_name = "default"
            if "lynai" in board_model.lower():
                has_lynai = conn.execute("SELECT 1 FROM profiles WHERE name = 'lynai'").fetchone()
                if has_lynai:
                    profile_name = "lynai"

            row = conn.execute(
                """
                INSERT INTO devices (device_id, name, profile_name, token, status, firmware_version, board_model)
                VALUES (%s, %s, %s, %s, 'pending', %s, %s)
                RETURNING *
                """,
                (norm_id, name, profile_name, token, firmware_version, board_model),
            ).fetchone()
            conn.commit()
            logger.info("Registered new pending board: device_id=%s name=%s profile=%s", norm_id, name, profile_name)
            return dict(row)

    async def ota_register_or_get(
        self,
        device_id: str | None,
        firmware_version: str = "",
        board_model: str = "",
    ) -> str:
        if not device_id or not device_id.strip():
            return self.fallback_registry.ota_token_for(None) if self.fallback_registry else ""
        rec = await asyncio.to_thread(
            self._ota_register_or_get_sync, device_id, firmware_version, board_model
        )
        return rec["token"]

    def _authenticate_sync(self, supplied_token: str, header_device_id: str | None) -> DeviceContext | None:
        pool = self._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT d.device_id, d.name, d.token, d.status,
                       p.name AS p_name, p.audio, p.vad, p.screen
                FROM devices d
                JOIN profiles p ON p.name = d.profile_name
                WHERE d.token = %s
                """,
                (supplied_token,),
            ).fetchone()
            if row is None or not hmac.compare_digest(supplied_token, row["token"]):
                return None
            if row["status"] != "approved":
                logger.info(
                    "Rejected board %s (%s) with status=%s",
                    row["name"], row["device_id"], row["status"],
                )
                return None
            conn.execute(
                "UPDATE devices SET last_seen_at = NOW() WHERE device_id = %s",
                (row["device_id"],),
            )
            conn.commit()
            profile = row_to_profile({
                "name": row["p_name"],
                "audio": row["audio"],
                "vad": row["vad"],
                "screen": row["screen"],
            })
            return DeviceContext(name=row["name"], device_id=row["device_id"], profile=profile)

    async def authenticate(self, supplied_token: str, header_device_id: str | None) -> DeviceContext | None:
        if not supplied_token:
            return None
        ctx = await asyncio.to_thread(self._authenticate_sync, supplied_token, header_device_id)
        if ctx is not None:
            return ctx
        # Optional fallback to static env tokens (e.g. legacy board 1 baked token)
        if self.fallback_registry:
            return self.fallback_registry.authenticate(supplied_token, header_device_id)
        return None

    def _get_profile_sync(self, name: str) -> BoardProfile | None:
        pool = self._require_pool()
        with pool.connection() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE name = %s", (name,)).fetchone()
            return row_to_profile(row) if row else None

    async def get_profile(self, name: str) -> BoardProfile | None:
        return await asyncio.to_thread(self._get_profile_sync, name)

    # --- Admin CRUD operations ---------------------------------------------------

    def _list_devices_sync(self, status: str | None = None) -> list[dict[str, Any]]:
        pool = self._require_pool()
        with pool.connection() as conn:
            if status in ("pending", "approved", "revoked"):
                rows = conn.execute(
                    "SELECT * FROM devices WHERE status = %s ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM devices
                    ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,
                             last_seen_at DESC
                    """
                ).fetchall()
            return [_serialize_device(r) for r in rows]

    async def list_devices(self, status: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_devices_sync, status)

    def _approve_device_sync(
        self,
        device_id: str,
        name: str | None = None,
        profile_name: str | None = None,
    ) -> dict[str, Any] | None:
        pool = self._require_pool()
        norm_id = device_id.strip().lower()
        with pool.connection() as conn:
            if profile_name:
                exists = conn.execute("SELECT 1 FROM profiles WHERE name = %s", (profile_name,)).fetchone()
                if not exists:
                    raise ValueError(f"Unknown profile: {profile_name}")
            row = conn.execute(
                """
                UPDATE devices
                SET status = 'approved',
                    approved_at = NOW(),
                    name = COALESCE(NULLIF(%s, ''), name),
                    profile_name = COALESCE(NULLIF(%s, ''), profile_name)
                WHERE device_id = %s
                RETURNING *
                """,
                ((name or "").strip(), (profile_name or "").strip(), norm_id),
            ).fetchone()
            conn.commit()
            return _serialize_device(row) if row else None

    async def approve_device(
        self,
        device_id: str,
        name: str | None = None,
        profile_name: str | None = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._approve_device_sync, device_id, name, profile_name)

    def _revoke_device_sync(self, device_id: str) -> dict[str, Any] | None:
        pool = self._require_pool()
        norm_id = device_id.strip().lower()
        with pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE devices
                SET status = 'revoked'
                WHERE device_id = %s
                RETURNING *
                """,
                (norm_id,),
            ).fetchone()
            conn.commit()
            return _serialize_device(row) if row else None

    async def revoke_device(self, device_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._revoke_device_sync, device_id)

    def _update_device_sync(
        self,
        device_id: str,
        name: str | None = None,
        profile_name: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        pool = self._require_pool()
        norm_id = device_id.strip().lower()
        with pool.connection() as conn:
            cur = conn.execute("SELECT * FROM devices WHERE device_id = %s", (norm_id,)).fetchone()
            if cur is None:
                return None, False
            new_prof = (profile_name or "").strip() or cur["profile_name"]
            if new_prof != cur["profile_name"]:
                exists = conn.execute("SELECT 1 FROM profiles WHERE name = %s", (new_prof,)).fetchone()
                if not exists:
                    raise ValueError(f"Unknown profile: {new_prof}")
            new_name = (name or "").strip() or cur["name"]
            row = conn.execute(
                """
                UPDATE devices
                SET name = %s, profile_name = %s
                WHERE device_id = %s
                RETURNING *
                """,
                (new_name, new_prof, norm_id),
            ).fetchone()
            conn.commit()
            return _serialize_device(row), (new_prof != cur["profile_name"])

    async def update_device(
        self,
        device_id: str,
        name: str | None = None,
        profile_name: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        return await asyncio.to_thread(self._update_device_sync, device_id, name, profile_name)

    def _delete_device_sync(self, device_id: str) -> bool:
        pool = self._require_pool()
        norm_id = device_id.strip().lower()
        with pool.connection() as conn:
            cur = conn.execute("DELETE FROM devices WHERE device_id = %s", (norm_id,))
            conn.commit()
            return cur.rowcount > 0

    async def delete_device(self, device_id: str) -> bool:
        return await asyncio.to_thread(self._delete_device_sync, device_id)

    def _list_profiles_sync(self) -> list[dict[str, Any]]:
        pool = self._require_pool()
        with pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT p.*, COUNT(d.device_id) AS device_count
                FROM profiles p
                LEFT JOIN devices d ON d.profile_name = p.name
                GROUP BY p.name
                ORDER BY CASE WHEN p.name = 'default' THEN 0 ELSE 1 END, p.name ASC
                """
            ).fetchall()
            return [_serialize_profile(r) for r in rows]

    async def list_profiles(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_profiles_sync)

    def _upsert_profile_sync(
        self,
        name: str,
        audio: Mapping[str, Any] | None,
        vad: Mapping[str, Any] | None,
        screen: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        pname = name.strip().lower()
        if not _PROFILE_NAME_RE.match(pname):
            raise ValueError("Profile name must be lowercase alphanumeric/underscore/hyphen (1-48 chars)")
        prof = BoardProfile(
            name=pname,
            audio=_merge_audio(audio, AudioConfig()),
            vad=_merge_vad(vad, VadConfig()),
            screen=_merge_screen(screen, ScreenConfig()),
            status=DEFAULT_STATUS_CATALOG,
        )
        a_dict, v_dict, s_dict = profile_to_dicts(prof)
        pool = self._require_pool()
        with pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO profiles (name, audio, vad, screen, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (name) DO UPDATE
                SET audio = EXCLUDED.audio,
                    vad = EXCLUDED.vad,
                    screen = EXCLUDED.screen,
                    updated_at = NOW()
                RETURNING *
                """,
                (pname, Jsonb(a_dict), Jsonb(v_dict), Jsonb(s_dict)),
            ).fetchone()
            count_row = conn.execute(
                "SELECT COUNT(*) AS c FROM devices WHERE profile_name = %s",
                (pname,),
            ).fetchone()
            conn.commit()
            result = dict(row)
            result["device_count"] = count_row["c"] if count_row else 0
            return _serialize_profile(result)

    async def upsert_profile(
        self,
        name: str,
        audio: Mapping[str, Any] | None,
        vad: Mapping[str, Any] | None,
        screen: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._upsert_profile_sync, name, audio, vad, screen)

    def _delete_profile_sync(self, name: str) -> bool:
        pname = name.strip().lower()
        if pname == "default":
            raise ValueError("Cannot delete the default profile")
        pool = self._require_pool()
        with pool.connection() as conn:
            in_use = conn.execute(
                "SELECT COUNT(*) AS c FROM devices WHERE profile_name = %s",
                (pname,),
            ).fetchone()
            if in_use and in_use["c"] > 0:
                raise ValueError(f"Profile '{pname}' is currently assigned to {in_use['c']} device(s)")
            cur = conn.execute("DELETE FROM profiles WHERE name = %s", (pname,))
            conn.commit()
            return cur.rowcount > 0

    async def delete_profile(self, name: str) -> bool:
        return await asyncio.to_thread(self._delete_profile_sync, name)
