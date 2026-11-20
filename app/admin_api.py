"""Internal-team Admin REST API & cookie authentication."""
import hashlib
import hmac
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.device_store import PostgresDeviceStore
from app.session import SessionManager

COOKIE_NAME = "xiaozhi_admin_session"
SESSION_TTL_SEC = 60 * 60 * 24 * 7  # 7 days


class LoginBody(BaseModel):
    password: str


class ApproveBody(BaseModel):
    name: str | None = None
    profile_name: str | None = None


class UpdateDeviceBody(BaseModel):
    name: str | None = None
    profile_name: str | None = None


class ProfileBody(BaseModel):
    name: str | None = None
    audio: dict[str, Any] = Field(default_factory=dict)
    vad: dict[str, Any] = Field(default_factory=dict)
    screen: dict[str, Any] = Field(default_factory=dict)


def _derive_signing_key(admin_password: str) -> bytes:
    return hashlib.sha256(f"xiaozhi-admin-v1:{admin_password}".encode("utf-8")).digest()


def _sign_session(admin_password: str) -> str:
    exp = str(int(time.time()) + SESSION_TTL_SEC)
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(_derive_signing_key(admin_password), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_session(cookie_val: str | None, admin_password: str) -> bool:
    if not cookie_val or not admin_password:
        return False
    parts = cookie_val.split(".")
    if len(parts) != 3:
        return False
    exp_s, nonce, supplied_sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    payload = f"{exp_s}.{nonce}"
    expected_sig = hmac.new(_derive_signing_key(admin_password), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied_sig, expected_sig)


def create_admin_router(
    store: PostgresDeviceStore | None,
    sessions: SessionManager,
    admin_password: str,
    is_production: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    def require_store() -> PostgresDeviceStore:
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Database is not configured (set DATABASE_URL to enable device management)",
            )
        return store

    def require_admin(request: Request) -> None:
        if not admin_password:
            raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured")
        cookie = request.cookies.get(COOKIE_NAME)
        if not _verify_session(cookie, admin_password):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @router.post("/login")
    async def login(body: LoginBody, response: Response):
        if not admin_password:
            raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured")
        if not hmac.compare_digest(body.password, admin_password):
            raise HTTPException(status_code=401, detail="Invalid password")
        token = _sign_session(admin_password)
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="lax",
            secure=is_production,
            max_age=SESSION_TTL_SEC,
            path="/",
        )
        return {"authenticated": True}

    @router.post("/logout")
    async def logout(response: Response):
        response.delete_cookie(key=COOKIE_NAME, path="/")
        return {"authenticated": False}

    @router.get("/me")
    async def me(_: None = Depends(require_admin)):
        return {"authenticated": True}

    # --- Devices -----------------------------------------------------------------

    @router.get("/devices")
    async def list_devices(
        status: str | None = None,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        devices = await db.list_devices(status=status)
        online_ids = set(sessions.active_device_ids())
        for d in devices:
            d["online"] = d["device_id"] in online_ids
        all_devices = devices if status is None else await db.list_devices(status=None)
        summary = {
            "total": len(all_devices),
            "online": sum(1 for d in all_devices if d["device_id"] in online_ids),
            "pending": sum(1 for d in all_devices if d["status"] == "pending"),
            "approved": sum(1 for d in all_devices if d["status"] == "approved"),
            "revoked": sum(1 for d in all_devices if d["status"] == "revoked"),
        }
        return {"devices": devices, "summary": summary}

    @router.post("/devices/{device_id}/approve")
    async def approve_device(
        device_id: str,
        body: ApproveBody | None = None,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        payload = body or ApproveBody()
        try:
            updated = await db.approve_device(
                device_id, name=payload.name, profile_name=payload.profile_name
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Device not found")
        updated["online"] = updated["device_id"] in set(sessions.active_device_ids())
        return updated

    @router.post("/devices/{device_id}/revoke")
    async def revoke_device(
        device_id: str,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        updated = await db.revoke_device(device_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="Device not found")
        disconnected = sessions.disconnect_device(updated["device_id"])
        updated["online"] = False
        updated["disconnected"] = disconnected
        return updated

    @router.patch("/devices/{device_id}")
    async def update_device(
        device_id: str,
        body: UpdateDeviceBody,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        try:
            updated, profile_changed = await db.update_device(
                device_id, name=body.name, profile_name=body.profile_name
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Device not found")
        disconnected = False
        if profile_changed:
            disconnected = sessions.disconnect_device(updated["device_id"])
        updated["online"] = (not disconnected) and (updated["device_id"] in set(sessions.active_device_ids()))
        updated["disconnected"] = disconnected
        return updated

    @router.delete("/devices/{device_id}")
    async def delete_device(
        device_id: str,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        norm_id = device_id.strip().lower()
        deleted = await db.delete_device(norm_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Device not found")
        sessions.disconnect_device(norm_id)
        sessions.clear_conversation(norm_id)
        return {"deleted": True, "device_id": norm_id}

    # --- Profiles ----------------------------------------------------------------

    @router.get("/profiles")
    async def list_profiles(
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        return {"profiles": await db.list_profiles()}

    @router.post("/profiles")
    async def create_profile(
        body: ProfileBody,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        if not body.name or not body.name.strip():
            raise HTTPException(status_code=400, detail="Profile name is required")
        try:
            prof = await db.upsert_profile(body.name, body.audio, body.vad, body.screen)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return prof

    @router.put("/profiles/{name}")
    async def update_profile(
        name: str,
        body: ProfileBody,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        try:
            prof = await db.upsert_profile(name, body.audio, body.vad, body.screen)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Disconnect active boards on this profile so they reconnect with updated tuning
        disconnected = sessions.disconnect_profile(prof["name"])
        prof["disconnected_devices"] = disconnected
        return prof

    @router.delete("/profiles/{name}")
    async def delete_profile(
        name: str,
        _: None = Depends(require_admin),
        db: PostgresDeviceStore = Depends(require_store),
    ):
        try:
            deleted = await db.delete_profile(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Profile not found")
        return {"deleted": True, "name": name}

    return router
