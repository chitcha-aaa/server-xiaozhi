import asyncio
import datetime
import logging
import os
import re
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn
import yaml

from app.admin_api import create_admin_router
from app.device_store import PostgresDeviceStore
from app.devices import DeviceRegistry
from app.langmind_client import LangMindClient
from app import screen_image
from app.session import DeviceSession, SessionManager

# Windows Thai console (cp874) can't print emoji/Thai mixed output; force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Configure Logging
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
# LOG_FILE keeps a copy on disk. Python writes it itself: piping the server through
# Tee-Object fails on PowerShell 5.1, which wraps a native program's stderr in a
# NativeCommandError and stops the script.
_log_file = os.environ.get("LOG_FILE", "").strip()
if _log_file:
    _log_handlers.append(logging.FileHandler(_log_file, mode="a", encoding="utf-8"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_log_handlers,
)
logger = logging.getLogger("XiaoZhiServer")

DEFAULT_VOICE_PREFIX = (
    "[คำสั่งระบบ: นี่คือการคุยด้วยเสียง ตอบคำถามทันทีเป็นภาษาพูด 1-2 ประโยคสั้นๆ "
    "ห้ามขึ้นต้นด้วยคำทักทายหรือแนะนำตัว ห้ามใช้อีโมจิหรือรายการ ถ้าคำถามไม่ชัดให้ถามกลับสั้นๆ ประโยคเดียว]\n"
)
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_DIST_DIR = os.path.join(BASE_DIR, "admin-ui", "dist")


def get_local_ip() -> str:
    """Helper to detect local LAN IP address instantly without blocking."""
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _parse_websocket_url(value: str):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    valid = (
        parsed.scheme in {"ws", "wss"}
        and bool(parsed.netloc)
        and parsed.path == "/ws"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )
    return parsed if valid else None


def load_config(path: str | None = None) -> dict:
    cfg_path = path or os.path.join(BASE_DIR, "config.yaml")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


async def _extract_ota_meta(request: Request) -> tuple[str, str]:
    """Extracts (firmware_version, board_model) from XiaoZhi /ota POST JSON body if present."""
    if request.method != "POST":
        return "", ""
    try:
        body = await request.json()
    except Exception:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    app_info = body.get("application") if isinstance(body.get("application"), dict) else {}
    board_info = body.get("board") if isinstance(body.get("board"), dict) else {}
    fw = str(app_info.get("version") or body.get("version") or "").strip()[:64]
    model = str(board_info.get("type") or board_info.get("name") or body.get("chip_model_name") or "").strip()[:64]
    return fw, model


def create_app(
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    langmind_client: Any = None,
    device_store: PostgresDeviceStore | None = None,
) -> FastAPI:
    """Build a self-contained XiaoZhi-LangMind FastAPI app for production or tests."""
    cfg = dict(config) if config is not None else load_config()
    environ = os.environ if env is None else env

    server_cfg = cfg.get("server") or {}
    langmind_cfg = cfg.get("langmind") or {}
    db_cfg = cfg.get("database") or {}
    admin_cfg = cfg.get("admin") or {}

    app_env = environ.get("APP_ENV", "development").strip().lower()
    is_production = app_env == "production"
    server_port = int(environ.get("PORT") or environ.get("XIAOZHI_PORT") or server_cfg.get("port", 8000))
    public_url = (environ.get("PUBLIC_URL") or server_cfg.get("public_url", "")).strip()

    langmind_base_url = environ.get(
        "LANGMIND_BASE_URL", langmind_cfg.get("base_url", "https://langmind.in.th/api")
    ).strip()
    langmind_api_key = environ.get("LANGMIND_API_KEY", "").strip()
    langmind_agent_id = (environ.get("LANGMIND_AGENT_ID") or langmind_cfg.get("agent_id", "")).strip()
    voice_speed = float(environ.get("VOICE_SPEED") or langmind_cfg.get("voice_speed", 1.0))
    voice_prefix = langmind_cfg.get("voice_prefix", DEFAULT_VOICE_PREFIX)

    database_url = (environ.get("DATABASE_URL") or db_cfg.get("url", "")).strip()
    admin_password = (environ.get("ADMIN_PASSWORD") or admin_cfg.get("password", "")).strip()

    timezone_name = (environ.get("TIMEZONE") or server_cfg.get("timezone", "Asia/Bangkok")).strip()
    timezone_offset_min = float(environ.get("TIMEZONE_OFFSET_MIN") or server_cfg.get("timezone_offset_min", 420))
    log_transcript = (
        environ.get("LOG_TRANSCRIPT", "").strip().lower()
        or str(server_cfg.get("log_transcript", "")).strip().lower()
    ) in ("1", "true", "yes", "on")
    screen_image_base = (
        environ.get("SCREEN_IMAGE_BASE_URL") or str(server_cfg.get("screen_image_base_url", ""))
    ).strip().rstrip("/")
    snapshot_dir = str(server_cfg.get("snapshot_dir", "")).strip() or "."
    conversation_ttl = int(server_cfg.get("conversation_ttl_sec", 600))
    max_ffmpeg_workers = max(1, int(environ.get("MAX_FFMPEG_WORKERS") or server_cfg.get("max_ffmpeg_workers", 6)))

    registry = DeviceRegistry.from_config(cfg, environ)
    store = device_store or (
        PostgresDeviceStore(database_url, fallback_registry=registry) if database_url else None
    )
    sessions = SessionManager(conversation_ttl=conversation_ttl, max_conversations=200)
    ffmpeg_sem = asyncio.Semaphore(max_ffmpeg_workers)

    langmind = langmind_client or LangMindClient(
        base_url=langmind_base_url,
        api_key=langmind_api_key,
        agent_id=langmind_agent_id,
        voice_speed=voice_speed,
        max_connections=40,
        max_keepalive_connections=20,
    )

    def runtime_ready() -> bool:
        if langmind_client is None and not (langmind_api_key and langmind_agent_id):
            return False
        if store is None and not registry.has_tokens():
            return False
        if is_production:
            parsed = _parse_websocket_url(public_url)
            try:
                lm_url = urlsplit(langmind_base_url)
                _ = lm_url.port
            except ValueError:
                return False
            return (
                parsed is not None
                and parsed.scheme == "wss"
                and lm_url.scheme == "https"
                and bool(lm_url.netloc)
                and not lm_url.username
                and not lm_url.password
                and not lm_url.query
                and not lm_url.fragment
            )
        return True

    def websocket_url(request: Request) -> str:
        if public_url:
            parsed = _parse_websocket_url(public_url)
            if parsed is None or (is_production and parsed.scheme != "wss"):
                raise HTTPException(status_code=503, detail="WebSocket endpoint is not configured")
            return public_url
        if is_production:
            raise HTTPException(status_code=503, detail="WebSocket endpoint is not configured")
        host = request.headers.get("host") or f"{get_local_ip()}:{server_port}"
        return f"ws://{host}/ws"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if store is not None:
            await store.init(extra_profiles=list(registry._profiles.values()))
        logger.info(
            "Bridge server started; runtime_ready=%s, db=%s, max_ffmpeg_workers=%d",
            runtime_ready(), bool(store), max_ffmpeg_workers,
        )
        try:
            yield
        finally:
            if store is not None:
                await store.aclose()
            if hasattr(langmind, "aclose"):
                await langmind.aclose()

    app = FastAPI(title="XiaoZhi-LangMind Bridge", lifespan=lifespan)
    app.state.registry = registry
    app.state.store = store
    app.state.sessions = sessions
    app.state.langmind = langmind
    app.state.ffmpeg_sem = ffmpeg_sem

    app.include_router(
        create_admin_router(
            store=store,
            sessions=sessions,
            admin_password=admin_password,
            is_production=is_production,
        )
    )

    @app.get("/healthz")
    async def healthz():
        if not runtime_ready():
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {
            "status": "ready",
            "active_devices": len(sessions.active_device_ids()),
            "database": bool(store),
        }

    @app.post("/screen/upload")
    async def screen_upload(request: Request):
        """Receives a screen snapshot pushed by a board (multipart, field "file", JPEG)."""
        form = await request.form()
        item = form.get("file")
        data = await item.read() if hasattr(item, "read") else (item or b"")
        if not data:
            raise HTTPException(status_code=400, detail="No image")
        raw_dev = request.query_params.get("device") or request.headers.get("device-id") or "device"
        safe_dev = _SAFE_NAME.sub("_", raw_dev).strip("_")[:32] or "device"
        filename = f"screen_{safe_dev}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.jpg"
        name = os.path.join(snapshot_dir, filename)
        with open(name, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("latin1"))
        logger.info("Screen snapshot saved: %s (%d bytes)", name, len(data))
        return {"saved": name, "bytes": len(data)}

    @app.get("/debug/snapshot")
    async def debug_snapshot():
        """Asks every connected board with screen.snapshot MCP tool to upload a snapshot."""
        base = screen_image_base or f"http://{get_local_ip()}:{server_port}"
        asked = await sessions.take_all_snapshots(base)
        return {"asked": asked, "base": base}

    async def _render_profile_slot(profile_name: str, slot: str) -> Response:
        profile = (await store.get_profile(profile_name)) if store is not None else None
        if profile is None:
            profile = registry.get_profile(profile_name)
        if profile is None:
            raise HTTPException(status_code=404, detail="Unknown profile")
        text = profile.status.text_for_slot(slot)
        if text is None:
            raise HTTPException(status_code=404, detail="Unknown status")
        png = await asyncio.to_thread(screen_image.render_status, text, profile.screen.size)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/screen/{slot}.png")
    async def screen_image_default_endpoint(slot: str):
        return await _render_profile_slot("default", slot)

    @app.get("/screen/{profile_name}/{slot}.png")
    async def screen_image_profile_endpoint(profile_name: str, slot: str):
        return await _render_profile_slot(profile_name, slot)

    @app.api_route("/ota/", methods=["GET", "POST"])
    @app.api_route("/ota", methods=["GET", "POST"])
    async def ota_endpoint(request: Request):
        """OTA endpoint called by XiaoZhi ESP32 on boot."""
        if not runtime_ready():
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        ws_url = websocket_url(request)
        device_id = request.headers.get("device-id")
        logger.info("OTA configuration requested (device-id=%s)", device_id or "none")

        # Firmware calls /ota/ only on boot, so reset the board's LangMind conversation.
        sessions.clear_conversation((device_id or "").strip().lower() or device_id)
        if store is not None:
            fw, model = await _extract_ota_meta(request)
            ota_token = await store.ota_register_or_get(device_id, firmware_version=fw, board_model=model)
        else:
            ota_token = registry.ota_token_for(device_id)

        now = datetime.datetime.now(datetime.timezone.utc)
        offset_min = int(round(timezone_offset_min))
        return JSONResponse(
            content={
                "firmware": {"version": "1.0.0", "url": ""},
                "server_time": {
                    "timestamp": int(now.timestamp() * 1000),
                    "timezone": timezone_name,
                    "timezone_offset": offset_min,
                },
                "websocket": {
                    "url": ws_url,
                    "token": ota_token,
                    "version": 1,
                },
            }
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        authorization = websocket.headers.get("authorization", "")
        scheme, _, supplied_token = authorization.partition(" ")
        if not runtime_ready():
            await websocket.close(code=1013)
            return
        if scheme.lower() == "bearer" and supplied_token:
            if store is not None:
                device = await store.authenticate(supplied_token, websocket.headers.get("device-id"))
            else:
                device = registry.authenticate(supplied_token, websocket.headers.get("device-id"))
        else:
            device = None
        if device is None:
            logger.warning("Rejected unauthenticated or unapproved WebSocket connection")
            await websocket.close(code=1008)
            return

        await websocket.accept()
        session = DeviceSession(
            websocket=websocket,
            device=device,
            langmind=langmind,
            sessions=sessions,
            ffmpeg_sem=ffmpeg_sem,
            voice_prefix=voice_prefix,
            screen_image_base=screen_image_base,
            log_transcript=log_transcript,
            local_ip_fn=get_local_ip,
            server_port=server_port,
        )
        await session.run()

    # --- Serve React Admin SPA at /admin -----------------------------------------
    assets_dir = os.path.join(ADMIN_DIST_DIR, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/admin/assets", StaticFiles(directory=assets_dir), name="admin-assets")

    @app.get("/admin")
    @app.get("/admin/{full_path:path}")
    async def serve_admin_spa(full_path: str = ""):
        index_path = os.path.join(ADMIN_DIST_DIR, "index.html")
        if not os.path.isfile(index_path):
            return JSONResponse(
                status_code=503,
                content={"detail": "Admin UI is not built yet. Run `npm install && npm run build` inside admin-ui/."},
            )
        return FileResponse(index_path)

    return app


_startup_config = load_config()
SERVER_HOST = _startup_config.get("server", {}).get("host", "0.0.0.0")
SERVER_PORT = int(
    os.environ.get("PORT")
    or os.environ.get("XIAOZHI_PORT")
    or _startup_config.get("server", {}).get("port", 8000)
)
app = create_app(config=_startup_config)


def main():
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
