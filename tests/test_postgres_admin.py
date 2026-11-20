"""End-to-end tests against real local PostgreSQL + Admin API + WebSocket enforcement."""
import json
import os
import threading
import time

from fastapi.testclient import TestClient
import psycopg
import pytest

from app.server import create_app

PG_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://xiaozhi:xiaozhi_local_dev@127.0.0.1:5432/xiaozhi",
)


class DummyLangMind:
    async def aclose(self) -> None:
        pass


def _pg_available() -> bool:
    try:
        with psycopg.connect(PG_URL, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="Local PostgreSQL not running")


@pytest.fixture
def clean_pg():
    """Wipes test rows before and after each test so tests are repeatable."""
    def _clean():
        with psycopg.connect(PG_URL, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS devices")
            conn.execute("DROP TABLE IF EXISTS profiles")
    _clean()
    yield PG_URL
    _clean()


def test_full_ota_pending_approve_revoke_and_profiles_flow(clean_pg):
    env = {
        "APP_ENV": "development",
        "DATABASE_URL": clean_pg,
        "ADMIN_PASSWORD": "team-secret-password",
        "LANGMIND_API_KEY": "test-key",
        "LANGMIND_AGENT_ID": "shared-agent",
    }
    app = create_app(config={}, env=env, langmind_client=DummyLangMind())

    with TestClient(app) as client:
        # 1. Health check reports database=True and ready
        h = client.get("/healthz")
        assert h.status_code == 200
        assert h.json()["database"] is True

        # 2. Admin UI SPA is served at /admin
        spa = client.get("/admin")
        assert spa.status_code == 200
        assert "XiaoZhi Fleet Admin" in spa.text

        # 3. Admin API rejects unauthenticated requests
        assert client.get("/api/admin/devices").status_code == 401
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401

        # 4. Admin login succeeds and sets HttpOnly cookie
        login = client.post("/api/admin/login", json={"password": "team-secret-password"})
        assert login.status_code == 200
        assert client.get("/api/admin/me").status_code == 200

        # 5. Built-in profiles (default, lynai) are seeded
        profs = client.get("/api/admin/profiles").json()["profiles"]
        names = {p["name"] for p in profs}
        assert {"default", "lynai"}.issubset(names)

        # 6. New board boots and calls POST /ota -> auto-registered as 'pending' with unique token
        mac = "44:bd:8d:7b:39:50"
        ota1 = client.post(
            "/ota/",
            headers={"Device-Id": mac},
            json={"application": {"version": "2.1.4"}, "board": {"type": "lynai"}},
        )
        assert ota1.status_code == 200
        token = ota1.json()["websocket"]["token"]
        assert len(token) == 32

        # Repeated /ota returns the SAME token (never rotates unexpectedly)
        ota2 = client.post("/ota/", headers={"Device-Id": mac})
        assert ota2.json()["websocket"]["token"] == token

        # 7. While 'pending', WebSocket /ws is REJECTED
        with pytest.raises(Exception):
            with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}", "Device-Id": mac}):
                pass

        # 8. Admin sees the board in Pending queue
        devs = client.get("/api/admin/devices").json()
        assert devs["summary"]["pending"] == 1
        assert devs["devices"][0]["device_id"] == mac
        assert devs["devices"][0]["profile_name"] == "lynai"  # inferred from board.type!

        # 9. Admin creates a new custom profile in Postgres via API
        created_prof = client.post(
            "/api/admin/profiles",
            json={
                "name": "esp32_s3_wide",
                "audio": {"sample_rate": 16000, "frame_duration_ms": 60},
                "vad": {"trailing_silence_ms": 600, "min_loud_speech_ms": 400, "trailing_rel_level": 0.25, "aggressiveness": 2},
                "screen": {"status_channel": "both", "screen_image": True, "size": [320, 240], "mcp": True},
            },
        )
        assert created_prof.status_code == 200

        # 10. Admin approves the board and assigns it a custom name + profile
        approved = client.post(
            f"/api/admin/devices/{mac}/approve",
            json={"name": "Lobby-Robot-01", "profile_name": "esp32_s3_wide"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["name"] == "Lobby-Robot-01"

        # 11. Now the board connects to /ws WITHOUT rebooting or re-calling /ota!
        with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}", "Device-Id": mac}) as ws:
            ws.send_text(json.dumps({"type": "hello", "version": 1}))
            hello = json.loads(ws.receive_text())
            assert hello["type"] == "hello"

            # Admin API shows board is online=True
            devs_online = client.get("/api/admin/devices").json()
            assert devs_online["summary"]["online"] == 1
            assert devs_online["devices"][0]["online"] is True

            # 12. Admin revokes the board -> immediately disconnects active WS!
            revoked = client.post(f"/api/admin/devices/{mac}/revoke")
            assert revoked.status_code == 200
            assert revoked.json()["status"] == "revoked"
            assert revoked.json()["disconnected"] is True

        # 13. Revoked board can NO LONGER connect to /ws
        with pytest.raises(Exception):
            with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}", "Device-Id": mac}):
                pass

        # 14. Admin re-approves -> board can connect again permanently
        reapproved = client.post(f"/api/admin/devices/{mac}/approve", json={})
        assert reapproved.status_code == 200
        with client.websocket_connect("/ws", headers={"Authorization": f"Bearer {token}", "Device-Id": mac}) as ws:
            ws.send_text(json.dumps({"type": "hello", "version": 1}))
            assert json.loads(ws.receive_text())["type"] == "hello"
