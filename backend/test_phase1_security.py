import datetime
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("backend"))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import config
from app.database import Base, get_db
from app.main import (
    app,
    create_kiosk_token,
    create_session_token,
    hash_password,
    init_users,
    is_item_visible_to_user,
    verify_password,
)
from app.models import AuditLog, EscalationDocument, Item, OvertimeRecord, User


test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class TestPhase1SecurityHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.orig_override = app.dependency_overrides.get(get_db)
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)
        cls._seed_fixtures()

    @classmethod
    def tearDownClass(cls):
        if cls.orig_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = cls.orig_override

    @classmethod
    def _seed_fixtures(cls):
        db = TestingSessionLocal()
        try:
            adonis = User(
                full_name="Adonis Lozada Figueroa",
                username="a.lozada",
                email="a.lozada@sycom.com.ve",
                role="coordinador",
                password_hash=hash_password("Coord$2026!"),
                is_active=1,
            )
            juan = User(
                full_name="Juan Fernandez",
                username="j.fernandez",
                email="j.fernandez@sycom.com.ve",
                role="especialista",
                password_hash=hash_password("Juan$2026!"),
                is_active=1,
            )
            luis = User(
                full_name="Luis Acosta",
                username="l.acosta",
                email="l.acosta@sycom.com.ve",
                role="analista",
                password_hash=hash_password("Luis$2026!"),
                is_active=1,
            )
            db.add_all([adonis, juan, luis])

            it_juan = Item(
                source="ClickUp",
                external_id="p1_item_juan",
                title="Tarea propia de Juan",
                assignee_name="Juan Fernandez",
                status="curso",
                priority="Media",
                client_name="SIMF",
            )
            it_adonis = Item(
                source="ClickUp",
                external_id="p1_item_adonis",
                title="Tarea exclusiva de Adonis",
                assignee_name="Adonis Lozada Figueroa",
                status="pendiente",
                priority="Alta",
                client_name="SIMF",
            )
            db.add_all([it_juan, it_adonis])

            esc = EscalationDocument(
                title="ESC-P1-001",
                category="PostgreSQL",
                doc_url="https://docs.google.com/document/d/p1_sec_test",
                uploaded_by="Adonis Lozada Figueroa",
                uploaded_by_role="Coordinador",
            )
            db.add(esc)

            now = datetime.datetime.utcnow().replace(microsecond=0)
            ot_adonis = OvertimeRecord(
                specialist_name="Adonis Lozada Figueroa",
                specialist_role="Coordinador",
                start_time=now - datetime.timedelta(hours=4),
                end_time=now - datetime.timedelta(hours=2),
                duration_minutes=120,
                duration_hours=2.0,
                description="Mantenimiento nocturno coordinador",
                category="PostgreSQL",
                project_name="SIMF",
                created_by="Adonis Lozada Figueroa",
                created_by_role="Coordinador",
            )
            ot_juan = OvertimeRecord(
                specialist_name="Juan Fernandez",
                specialist_role="Especialista",
                start_time=now - datetime.timedelta(hours=3),
                end_time=now - datetime.timedelta(hours=2),
                duration_minutes=60,
                duration_hours=1.0,
                description="Soporte guardia especialista",
                category="Servidores",
                project_name="SIMF",
                created_by="Juan Fernandez",
                created_by_role="Especialista",
            )
            db.add_all([ot_adonis, ot_juan])

            log_adonis = AuditLog(
                username="a.lozada",
                user_fullname="Adonis Lozada Figueroa",
                user_role="coordinador",
                action="LOGIN_SUCCESS",
                entity_type="auth",
                entity_id="a.lozada",
                details="Coord login",
            )
            log_juan = AuditLog(
                username="j.fernandez",
                user_fullname="Juan Fernandez",
                user_role="especialista",
                action="CREATE_ITEM",
                entity_type="item",
                entity_id="p1_item_juan",
                details="Juan action",
            )
            db.add_all([log_adonis, log_juan])
            db.commit()

            cls.item_juan_id = it_juan.id
            cls.item_adonis_id = it_adonis.id
            cls.esc_id = esc.id
            cls.ot_adonis_id = ot_adonis.id
            cls.ot_juan_id = ot_juan.id
        finally:
            db.close()

    def test_01_docs_and_openapi_disabled_in_production(self):
        self.assertFalse(config.ENABLE_API_DOCS)
        for path in ("/docs", "/redoc", "/openapi.json"):
            res = self.client.get(path)
            self.assertEqual(res.status_code, 404, f"Expected 404 for {path}, got {res.status_code}")

    def test_02_cors_whitelist_not_wildcard(self):
        self.assertNotIn("*", config.CORS_ALLOWED_ORIGINS)
        evil_res = self.client.get("/api/health", headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(evil_res.status_code, 200)
        self.assertNotEqual(evil_res.headers.get("access-control-allow-origin"), "*")
        self.assertNotEqual(evil_res.headers.get("access-control-allow-origin"), "https://attacker.invalid")

        allowed_origin = config.CORS_ALLOWED_ORIGINS[0]
        ok_res = self.client.get("/api/health", headers={"Origin": allowed_origin})
        self.assertEqual(ok_res.headers.get("access-control-allow-origin"), allowed_origin)

    def test_03_compose_and_config_have_no_weak_hardcoded_secrets(self):
        root_dir = Path(__file__).resolve().parent.parent
        compose_text = (root_dir / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("expose:", compose_text)
        self.assertNotIn("8000:8000", compose_text)
        self.assertNotIn("tasks_secret_2026", compose_text)

        env_example_text = (root_dir / ".env.example").read_text(encoding="utf-8")
        backend_env_example = (root_dir / "backend" / ".env.example").read_text(encoding="utf-8")
        self.assertNotIn("tasks_secret_2026", env_example_text)
        self.assertNotIn("sycom2026", backend_env_example)

        # init_users with empty DEFAULT_INIT_PASSWORD must not seed 'sycom2026'
        mem_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=mem_engine)
        SessionLocal = sessionmaker(bind=mem_engine)
        mem_db = SessionLocal()
        try:
            orig_default = config.DEFAULT_INIT_PASSWORD
            try:
                import app.main as main_mod
                main_mod.DEFAULT_INIT_PASSWORD = ""
                with patch("app.main.SessionLocal", SessionLocal):
                    init_users()
            finally:
                main_mod.DEFAULT_INIT_PASSWORD = orig_default
            seeded = mem_db.query(User).all()
            self.assertGreater(len(seeded), 0)
            for u in seeded:
                self.assertTrue(u.password_hash)
                self.assertFalse(verify_password("sycom2026", u.password_hash))
        finally:
            mem_db.close()

    @classmethod
    def _auth_headers_for(cls, username: str) -> dict[str, str]:
        db = TestingSessionLocal()
        try:
            u = db.query(User).filter(User.username == username).first()
            assert u is not None, f"Seeded user {username} missing"
            return {"Authorization": f"Bearer {create_session_token(u)}"}
        finally:
            db.close()

    def test_04_unauthenticated_requests_rejected_fail_closed(self):
        protected_gets = [
            "/api/items",
            "/api/items?tv_mode=true",
            f"/api/items/{self.item_adonis_id}",
            "/api/items/99999999",
            "/api/kpis",
            "/api/team",
            "/api/clients",
            "/api/ibps",
            "/api/users",
            "/api/auth/users",
            "/api/users/me",
            "/api/escalations",
            "/api/overtimes",
            "/api/overtimes/analytics",
            f"/api/overtimes/{self.ot_adonis_id}",
            "/api/audit-logs",
            "/api/notifications/telegram-status",
        ]
        for path in protected_gets:
            res = self.client.get(path)
            self.assertEqual(res.status_code, 401, f"GET {path} allowed unauthenticated ({res.status_code})")

        protected_writes = [
            ("POST", "/api/sync", None),
            ("PUT", "/api/users/me", {"phone": "1234"}),
            ("POST", "/api/escalations", {"title": "X", "category": "CPU", "doc_url": "https://docs.google.com/d/1"}),
            ("PUT", f"/api/escalations/{self.esc_id}", {"title": "Y"}),
            ("DELETE", f"/api/escalations/{self.esc_id}", None),
            ("POST", "/api/overtimes", {
                "specialist_name": "Adonis Lozada Figueroa",
                "start_time": "2026-11-12T18:00:00",
                "end_time": "2026-11-12T19:00:00",
                "description": "Unauthorized test",
            }),
        ]
        for method, path, body in protected_writes:
            res = self.client.request(method, path, json=body)
            self.assertEqual(res.status_code, 401, f"{method} {path} allowed unauthenticated ({res.status_code})")

    def test_05_forged_unknown_or_publico_role_rejected(self):
        for bad_role in ("publico", "guest", "superadmin_fake", "coordinador"):
            headers = {"X-User-Role": bad_role, "X-User-Name": "Intruder"}
            self.assertEqual(self.client.get("/api/items", headers=headers).status_code, 401)
            self.assertEqual(self.client.get("/api/kpis", headers=headers).status_code, 401)
            self.assertEqual(self.client.get("/api/escalations", headers=headers).status_code, 401)
            self.assertEqual(self.client.get("/api/users/me", headers=headers).status_code, 401)

    def test_06_tv_guest_role_is_strictly_read_only(self):
        tv_headers = {"Authorization": f"Bearer {create_kiosk_token()}"}

        # Allowed TV display read endpoints
        for read_path in (
            "/api/items?tv_mode=true",
            "/api/kpis",
            "/api/reports/data",
            "/api/ibps",
            "/api/team",
        ):
            res = self.client.get(read_path, headers=tv_headers)
            self.assertEqual(res.status_code, 200, f"TV read failed for {read_path}: {res.text}")

        # Denied privileged / operational / write endpoints
        denied = [
            ("POST", "/api/sync", None),
            ("GET", "/api/escalations", None),
            ("GET", "/api/users", None),
            ("GET", "/api/auth/users", None),
            ("GET", "/api/users/me", None),
            ("PUT", "/api/users/me", {"bio": "hijack"}),
            ("POST", "/api/escalations", {"title": "TV Hack", "category": "CPU", "doc_url": "https://docs.google.com/d/1"}),
            ("GET", "/api/overtimes", None),
            ("GET", "/api/overtimes/analytics", None),
            ("GET", "/api/audit-logs", None),
            ("GET", "/api/notifications/telegram-status", None),
        ]
        for method, path, body in denied:
            res = self.client.request(method, path, headers=tv_headers, json=body)
            self.assertEqual(res.status_code, 403, f"TV role should be denied {method} {path}, got {res.status_code}")

    def test_07_specialist_idor_and_privilege_boundaries(self):
        spec_headers = self._auth_headers_for("j.fernandez")

        # Coordinator-only endpoints denied
        self.assertEqual(self.client.get("/api/auth/users", headers=spec_headers).status_code, 403)
        self.assertEqual(self.client.get("/api/notifications/telegram-status", headers=spec_headers).status_code, 403)

        # Cannot query another user's profile via ?username=
        idor_profile = self.client.get("/api/users/me?username=a.lozada", headers=spec_headers)
        self.assertEqual(idor_profile.status_code, 403)

        # Cannot read another specialist/coordinator's overtime by detail ID
        self.assertEqual(self.client.get(f"/api/overtimes/{self.ot_adonis_id}", headers=spec_headers).status_code, 403)
        self.assertEqual(self.client.get(f"/api/overtimes/{self.ot_juan_id}", headers=spec_headers).status_code, 200)

        # Spoofed ?specialist= query param is ignored for non-coordinator
        ot_list = self.client.get("/api/overtimes?specialist=Adonis%20Lozada%20Figueroa", headers=spec_headers)
        self.assertEqual(ot_list.status_code, 200)
        for rec in ot_list.json():
            self.assertIn("juan", rec["specialist_name"].lower())

        # Audit logs scoped to self only (and forged header without token rejected)
        logs_res = self.client.get("/api/audit-logs", headers=spec_headers)
        self.assertEqual(logs_res.status_code, 200)
        for entry in logs_res.json():
            self.assertNotEqual(entry["username"], "a.lozada")

        no_auth_logs = self.client.get("/api/audit-logs", headers={"X-User-Role": "especialista"})
        self.assertEqual(no_auth_logs.status_code, 401)

    def test_08_coordinator_checks_and_url_scheme_validation(self):
        coord_headers = self._auth_headers_for("a.lozada")

        # Telegram status is coordinator-only and never leaks token/chat_id substrings
        orig_tok, orig_cid = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID
        try:
            config.TELEGRAM_BOT_TOKEN = "999999:SUPER_SECRET_BOT_TOKEN"
            config.TELEGRAM_CHAT_ID = "-1001234567890"
            tg_res = self.client.get("/api/notifications/telegram-status", headers=coord_headers)
            self.assertEqual(tg_res.status_code, 200)
            tg_body = tg_res.json()
            self.assertTrue(tg_body.get("bot_configured"))
            self.assertTrue(tg_body.get("chat_configured"))
            self.assertEqual(tg_body.get("bot_token_preview"), "Configurado")
            self.assertEqual(tg_body.get("chat_id_preview"), "Configurado")
            self.assertNotIn("SUPER_SECRET", str(tg_body))
            self.assertNotIn("999999", str(tg_body))
            self.assertNotIn("-1001234", str(tg_body))
        finally:
            config.TELEGRAM_BOT_TOKEN = orig_tok
            config.TELEGRAM_CHAT_ID = orig_cid

        # Rejects javascript: XSS scheme in escalation doc_url
        bad_url_res = self.client.post(
            "/api/escalations",
            json={
                "title": "XSS Link Attempt",
                "category": "CPU",
                "doc_url": "javascript:alert(1)",
            },
            headers=coord_headers,
        )
        self.assertEqual(bad_url_res.status_code, 400)

    def test_09_is_item_visible_to_user_fail_closed(self):
        item_juan = Item(assignee_name="Juan Fernandez")
        self.assertFalse(is_item_visible_to_user(item_juan, "", "Juan Fernandez"))
        self.assertFalse(is_item_visible_to_user(item_juan, "publico", "Juan Fernandez"))
        self.assertFalse(is_item_visible_to_user(item_juan, "especialista", ""))
        self.assertTrue(is_item_visible_to_user(item_juan, "especialista", "Juan Fernandez"))
        self.assertFalse(is_item_visible_to_user(item_juan, "analista", "Luis Acosta"))

    def test_10_frontend_tv_mode_and_sync_hardening(self):
        root_dir = Path(__file__).resolve().parent.parent
        html = (root_dir / "frontend" / "index.html").read_text(encoding="utf-8")

        # Kiosk TV mode uses read-only 'tv' role and requires /api/auth/kiosk
        kiosk_start = html.index("async function activateKioskSession(")
        kiosk_block = html[kiosk_start:kiosk_start + 600]
        self.assertIn("role: 'tv'", kiosk_block)
        self.assertNotIn("role: 'coordinador'", kiosk_block)
        self.assertIn("/auth/kiosk", html)

        # TV sync button has explicit ID and forceSync sends auth headers + status check
        self.assertIn('id="tvBtnSync"', html)
        force_start = html.index("async function forceSync()")
        force_block = html[force_start:force_start + 700]
        self.assertIn("headers: getAuthHeaders()", force_block)
        self.assertIn("!syncRes.ok", force_block)

        # TV init skips fetchCurrentUserProfile and verifies server session
        auth_start = html.index("async function checkAuthAndInit()")
        auth_block = html[auth_start:auth_start + 3200]
        self.assertIn("roleLower === 'tv'", auth_block)
        self.assertIn("fetchCurrentUserProfile();", auth_block)


if __name__ == "__main__":
    unittest.main()
