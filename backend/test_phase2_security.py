import base64
import datetime
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath("backend"))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_mod
from app import config
from app.database import Base, get_db
from app.models import EscalationDocument, Item, User
from app.reports.report_generator import generate_executive_html


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


class TestPhase2SecurityHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.orig_override = main_mod.app.dependency_overrides.get(get_db)
        main_mod.app.dependency_overrides[get_db] = override_get_db
        cls.tmp_upload_dir = tempfile.TemporaryDirectory()
        cls.orig_upload_dir = main_mod.UPLOAD_ESCALATIONS_DIR
        main_mod.UPLOAD_ESCALATIONS_DIR = cls.tmp_upload_dir.name
        cls.client = TestClient(main_mod.app)

    @classmethod
    def tearDownClass(cls):
        main_mod.UPLOAD_ESCALATIONS_DIR = cls.orig_upload_dir
        cls.tmp_upload_dir.cleanup()
        if cls.orig_override is None:
            main_mod.app.dependency_overrides.pop(get_db, None)
        else:
            main_mod.app.dependency_overrides[get_db] = cls.orig_override
        test_engine.dispose()

    def setUp(self):
        main_mod._clear_rate_limiters()
        self.orig_kiosk_secret = config.KIOSK_SECRET
        self.orig_kiosk_secret_main = main_mod.KIOSK_SECRET
        self.orig_kiosk_ips = list(config.KIOSK_ALLOWED_IPS)
        self.orig_kiosk_ips_main = list(main_mod.KIOSK_ALLOWED_IPS)

        db = TestingSessionLocal()
        try:
            db.query(EscalationDocument).delete()
            db.query(Item).delete()
            db.query(User).delete()
            pw_hash = main_mod.hash_password("Coord$2026!")
            db.add_all([
                User(
                    full_name="Adonis Lozada",
                    username="a.lozada",
                    email="a.lozada@sycom.com.ve",
                    role="coordinador",
                    password_hash=pw_hash,
                    must_change_password=0,
                    token_version=1,
                    is_active=1,
                ),
                User(
                    full_name="Juan Fernández",
                    username="j.fernandez",
                    email="j.fernandez@sycom.com.ve",
                    role="especialista",
                    password_hash=pw_hash,
                    must_change_password=0,
                    token_version=1,
                    is_active=1,
                ),
                User(
                    full_name="Luis Acosta",
                    username="l.acosta",
                    email="l.acosta@sycom.com.ve",
                    role="analista",
                    password_hash=pw_hash,
                    must_change_password=0,
                    token_version=1,
                    is_active=1,
                ),
            ])
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        main_mod._clear_rate_limiters()
        config.KIOSK_SECRET = self.orig_kiosk_secret
        main_mod.KIOSK_SECRET = self.orig_kiosk_secret_main
        config.KIOSK_ALLOWED_IPS = self.orig_kiosk_ips
        main_mod.KIOSK_ALLOWED_IPS = self.orig_kiosk_ips_main
        self.client.cookies.clear()

    def _login_bearer(self, username: str, password: str = "Coord$2026!") -> dict[str, str]:
        res = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(res.status_code, 200, res.text)
        self.client.cookies.clear()
        return {"Authorization": f"Bearer {res.json()['access_token']}"}

    def test_01_argon2id_hashing_and_legacy_sha256_transparent_rehash(self):
        argon_hash = main_mod.hash_password("MyStrong$Pass2026")
        self.assertTrue(argon_hash.startswith("$argon2id$"))
        self.assertTrue(main_mod.verify_password("MyStrong$Pass2026", argon_hash))
        self.assertFalse(main_mod.verify_password("Wrong$Pass2026", argon_hash))

        db = TestingSessionLocal()
        try:
            u = User(
                full_name="Usuario Legacy",
                username="legacy.user",
                email="legacy@sycom.com.ve",
                role="especialista",
                password_hash=main_mod._legacy_sha256_hash("Legacy$2026!"),
                must_change_password=0,
                is_active=1,
            )
            db.add(u)
            db.commit()
            self.assertFalse(u.password_hash.startswith("$argon2"))
        finally:
            db.close()

        login_res = self.client.post(
            "/api/auth/login",
            json={"username": "legacy.user", "password": "Legacy$2026!"},
        )
        self.assertEqual(login_res.status_code, 200, login_res.text)

        db = TestingSessionLocal()
        try:
            u = db.query(User).filter(User.username == "legacy.user").first()
            self.assertTrue(u.password_hash.startswith("$argon2id$"))
        finally:
            db.close()

        # Confirm upgraded Argon2id hash authenticates again cleanly
        self.client.cookies.clear()
        relogin_res = self.client.post(
            "/api/auth/login",
            json={"username": "legacy.user", "password": "Legacy$2026!"},
        )
        self.assertEqual(relogin_res.status_code, 200)

    def test_02_password_policy_8_to_128_and_complexity(self):
        headers = self._login_bearer("j.fernandez")
        invalid_candidates = [
            "Ab$12",               # too short (< 8)
            "Aa1$" + ("x" * 126),  # too long (> 128)
            "12345678$",           # no letter
            "Abcdefgh$",           # no digit
            "Abcdefg123",          # no special char
        ]
        for cand in invalid_candidates:
            res = self.client.post(
                "/api/auth/change-password",
                json={"old_password": "Coord$2026!", "new_password": cand},
                headers=headers,
            )
            self.assertEqual(res.status_code, 400, f"Weak/invalid password accepted: {cand!r}")

        # Passphrase > 15 chars (52 chars) must succeed in Phase 2
        long_passphrase = "InfraestructuraSegura$2026!PassphraseLarga52Chars!!"
        self.assertGreater(len(long_passphrase), 15)
        ok_res = self.client.post(
            "/api/auth/change-password",
            json={"old_password": "Coord$2026!", "new_password": long_passphrase},
            headers=headers,
        )
        self.assertEqual(ok_res.status_code, 200, ok_res.text)
        relogin = self.client.post(
            "/api/auth/login",
            json={"username": "j.fernandez", "password": long_passphrase},
        )
        self.assertEqual(relogin.status_code, 200)

    def test_03_account_lockout_and_ip_rate_limiting(self):
        # 5 wrong attempts for existing account triggers lockout (429)
        for idx in range(main_mod.MAX_LOGIN_USER_FAILURES):
            res = self.client.post(
                "/api/auth/login",
                json={"username": "j.fernandez", "password": f"Wrong$Pass{idx}"},
            )
            if idx < main_mod.MAX_LOGIN_USER_FAILURES - 1:
                self.assertEqual(res.status_code, 401)
            else:
                self.assertEqual(res.status_code, 429)

        # Even valid password fails while locked out
        blocked_valid = self.client.post(
            "/api/auth/login",
            json={"username": "j.fernandez", "password": "Coord$2026!"},
        )
        self.assertEqual(blocked_valid.status_code, 429)

        # Reset lockouts cleanly and test per-IP rate limiting across distinct non-existent users
        main_mod._clear_rate_limiters()
        for idx in range(main_mod.MAX_LOGIN_IP_FAILURES):
            r = self.client.post(
                "/api/auth/login",
                json={"username": f"nonexistent_{idx}", "password": "Bad$Password1"},
            )
            self.assertEqual(r.status_code, 401)

        ip_limited = self.client.post(
            "/api/auth/login",
            json={"username": "a.lozada", "password": "Coord$2026!"},
        )
        self.assertEqual(ip_limited.status_code, 429)

    def test_04_must_change_password_enforced_until_changed(self):
        db = TestingSessionLocal()
        try:
            u = User(
                full_name="Temporal User",
                username="temp.user",
                email="temp@sycom.com.ve",
                role="especialista",
                password_hash=main_mod.hash_password("TempInit$2026"),
                must_change_password=1,
                token_version=1,
                is_active=1,
            )
            db.add(u)
            db.commit()
        finally:
            db.close()

        login_res = self.client.post(
            "/api/auth/login",
            json={"username": "temp.user", "password": "TempInit$2026"},
        )
        self.assertEqual(login_res.status_code, 200)
        body = login_res.json()
        self.assertTrue(body["must_change_password"])
        old_token = body["access_token"]
        self.client.cookies.clear()

        # Session endpoint reflects pending mandatory change
        sess_res = self.client.get(
            "/api/auth/session",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(sess_res.status_code, 200)
        self.assertTrue(sess_res.json()["must_change_password"])

        # All operational endpoints reject until password is changed
        for path in ("/api/items", "/api/kpis", "/api/users/me", "/api/escalations"):
            r = self.client.get(path, headers={"Authorization": f"Bearer {old_token}"})
            self.assertEqual(r.status_code, 403, f"{path} should be blocked while must_change_password=1")

        # Complete forced password change
        ch_res = self.client.post(
            "/api/auth/change-password",
            json={"old_password": "TempInit$2026", "new_password": "PersonalStrong$2026"},
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(ch_res.status_code, 200)
        new_token = ch_res.json()["access_token"]

        # Old token invalidated via token_version increment; new token granted full operational access
        self.assertEqual(
            self.client.get("/api/items", headers={"Authorization": f"Bearer {old_token}"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/api/items", headers={"Authorization": f"Bearer {new_token}"}).status_code,
            200,
        )

    def test_05_cookie_attributes_csrf_and_logout_revocation(self):
        login_res = self.client.post(
            "/api/auth/login",
            json={"username": "a.lozada", "password": "Coord$2026!"},
        )
        self.assertEqual(login_res.status_code, 200)
        bearer_token = login_res.json()["access_token"]

        set_cookie = login_res.headers.get("set-cookie", "").lower()
        self.assertIn(f"{config.SESSION_COOKIE_NAME}=", set_cookie)
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=lax", set_cookie)
        self.assertNotIn("; secure", set_cookie)

        # Cookie read works without X-Requested-With
        self.assertEqual(self.client.get("/api/auth/session").status_code, 200)

        # Cookie mutation without X-Requested-With fails CSRF guard
        no_csrf = self.client.put("/api/users/me", json={"bio": "No CSRF"})
        self.assertEqual(no_csrf.status_code, 403)

        # Cookie mutation with bad Origin fails CSRF guard even with X-Requested-With
        bad_origin = self.client.put(
            "/api/users/me",
            json={"bio": "Evil Origin"},
            headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://evil.attacker.invalid"},
        )
        self.assertEqual(bad_origin.status_code, 403)

        # Cookie mutation with X-Requested-With and valid Origin succeeds
        ok_csrf = self.client.put(
            "/api/users/me",
            json={"bio": "Updated safely via cookie"},
            headers={"X-Requested-With": "XMLHttpRequest", "Origin": "http://localhost:7070"},
        )
        self.assertEqual(ok_csrf.status_code, 200)

        # Logout with revoke_all=true clears cookie AND revokes prior Bearer token
        logout_no_csrf = self.client.post("/api/auth/logout?revoke_all=true")
        self.assertEqual(logout_no_csrf.status_code, 403)

        logout_ok = self.client.post(
            "/api/auth/logout?revoke_all=true",
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(logout_ok.status_code, 200)
        self.assertEqual(
            self.client.get("/api/items", headers={"Authorization": f"Bearer {bearer_token}"}).status_code,
            401,
        )

    def test_06_spoofed_headers_and_rbac_fail_closed(self):
        spoofed = {
            "X-User-Role": "coordinador",
            "X-User-Name": "Adonis Lozada",
            "X-User-Username": "a.lozada",
        }
        self.assertEqual(self.client.get("/api/items", headers=spoofed).status_code, 401)
        self.assertEqual(self.client.get("/api/users", headers=spoofed).status_code, 401)

        # Specialist with real Bearer token CANNOT escalate via spoofed header or query params
        spec_headers = self._login_bearer("j.fernandez")
        combined_spoof = {**spec_headers, **spoofed}
        self.assertEqual(self.client.get("/api/auth/users?role=coordinador&user=Adonis", headers=combined_spoof).status_code, 403)
        self.assertEqual(
            self.client.get("/api/reports/history?role=coordinador&user=Adonis", headers=combined_spoof).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/reports/generate-and-save?role=coordinador&user=Adonis",
                json={"title": "Escalation Attempt"},
                headers=combined_spoof,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/notifications/telegram-status?role=coordinador", headers=combined_spoof).status_code,
            403,
        )

    def test_07_escalation_pdf_hardening_and_url_ssrf_defense(self):
        coord_headers = self._login_bearer("a.lozada")

        # 1. Reject fake PDF (HTML payload masquerading as .pdf)
        fake_b64 = base64.b64encode(b"<html><script>alert(1)</script></html>").decode()
        bad_magic = self.client.post(
            "/api/escalations/upload",
            json={"title": "Fake", "category": "CPU", "filename": "fake.pdf", "file_base64": fake_b64},
            headers=coord_headers,
        )
        self.assertEqual(bad_magic.status_code, 400)

        # 2. Reject non-.pdf filename even with %PDF- header
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        bad_ext = self.client.post(
            "/api/escalations/upload",
            json={"title": "HTML Ext", "category": "CPU", "filename": "payload.html", "file_base64": pdf_b64},
            headers=coord_headers,
        )
        self.assertEqual(bad_ext.status_code, 400)

        # 3. Valid PDF with traversal filename gets stored strictly as <uuid4_hex>.pdf inside UPLOAD_ESCALATIONS_DIR
        ok_up = self.client.post(
            "/api/escalations/upload",
            json={
                "title": "Valid PDF Upload",
                "category": "PostgreSQL",
                "filename": '../../evil"<script>.pdf',
                "file_base64": pdf_b64,
            },
            headers=coord_headers,
        )
        self.assertEqual(ok_up.status_code, 200, ok_up.text)
        doc_id = ok_up.json()["document"]["id"]

        db = TestingSessionLocal()
        try:
            doc = db.query(EscalationDocument).filter(EscalationDocument.id == doc_id).first()
            self.assertRegex(doc.stored_filename, r"^[0-9a-f]{32}\.pdf$")
            stored_path = (Path(self.tmp_upload_dir.name) / doc.stored_filename).resolve()
            self.assertTrue(stored_path.is_file())
            self.assertTrue(stored_path.is_relative_to(Path(self.tmp_upload_dir.name).resolve()))
        finally:
            db.close()

        # 4. /view and /download require auth and return nosniff + sanitized filename
        self.assertEqual(self.client.get(f"/api/escalations/{doc_id}/view").status_code, 401)
        self.assertEqual(self.client.get(f"/api/escalations/{doc_id}/download").status_code, 401)

        view_res = self.client.get(f"/api/escalations/{doc_id}/view", headers=coord_headers)
        self.assertEqual(view_res.status_code, 200)
        self.assertEqual(view_res.headers.get("x-content-type-options"), "nosniff")
        self.assertIn("application/pdf", view_res.headers.get("content-type", ""))

        dl_res = self.client.get(f"/api/escalations/{doc_id}/download", headers=coord_headers)
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.headers.get("x-content-type-options"), "nosniff")
        disp = dl_res.headers.get("content-disposition", "")
        self.assertIn("attachment", disp)
        self.assertNotIn("<script>", disp)

        # 5. Direct path traversal helper rejection
        for bad_name in ("../secret.txt", "..\\secret.txt", "subdir/test.pdf", ""):
            with self.assertRaises(main_mod.HTTPException):
                main_mod._resolve_safe_escalation_path(bad_name)

        # 6. Anti-SSRF / strict https:// URL validation
        bad_urls = [
            "http://docs.google.com/document/d/1",
            "javascript:alert(1)",
            "https://localhost/admin",
            "https://127.0.0.1/internal",
            "https://10.0.0.5/secret",
            "https://192.168.1.10/router",
            "https://user:pass@docs.google.com/d/1",
        ]
        for bad_u in bad_urls:
            r = self.client.post(
                "/api/escalations",
                json={"title": "Bad URL", "category": "CPU", "doc_url": bad_u},
                headers=coord_headers,
            )
            self.assertEqual(r.status_code, 400, f"Unsafe URL should fail: {bad_u}")

    def test_08_kiosk_secret_ip_allowlist_and_strict_read_only_scope(self):
        # Unconfigured kiosk secret -> 403
        config.KIOSK_SECRET = ""
        main_mod.KIOSK_SECRET = ""
        self.assertEqual(self.client.post("/api/auth/kiosk", json={"secret": "anything"}).status_code, 403)

        # Configure kiosk secret
        config.KIOSK_SECRET = "Kiosk$Secret#2026"
        main_mod.KIOSK_SECRET = "Kiosk$Secret#2026"

        # IP outside allowlist -> 403
        config.KIOSK_ALLOWED_IPS = ["10.99.99.0/24"]
        main_mod.KIOSK_ALLOWED_IPS = ["10.99.99.0/24"]
        self.assertEqual(
            self.client.post("/api/auth/kiosk", json={"secret": "Kiosk$Secret#2026"}).status_code,
            403,
        )

        # Allow local TestClient IP, fail wrong secret -> 401, then valid secret -> 200
        config.KIOSK_ALLOWED_IPS = ["127.0.0.1/32", "testclient"]
        main_mod.KIOSK_ALLOWED_IPS = []  # allow all / testclient cleanly after IP check test
        self.assertEqual(
            self.client.post("/api/auth/kiosk", json={"secret": "wrong"}).status_code,
            401,
        )

        k_res = self.client.post("/api/auth/kiosk", json={"secret": "Kiosk$Secret#2026"})
        self.assertEqual(k_res.status_code, 200, k_res.text)
        k_tok = k_res.json()["access_token"]
        k_headers = {"Authorization": f"Bearer {k_tok}"}
        self.client.cookies.clear()

        # Allowed read-only display endpoints
        for path in ("/api/items?tv_mode=true", "/api/kpis", "/api/team", "/api/clients", "/api/ibps", "/api/reports/data"):
            r = self.client.get(path, headers=k_headers)
            self.assertEqual(r.status_code, 200, f"Kiosk allowed path failed {path}: {r.text}")

        # Blocked internal / admin / mutation endpoints
        for method, path in [
            ("GET", "/api/escalations"),
            ("GET", "/api/overtimes"),
            ("GET", "/api/users"),
            ("GET", "/api/users/me"),
            ("GET", "/api/audit-logs"),
            ("GET", "/api/reports/excel"),
            ("GET", "/api/reports/print-view"),
            ("POST", "/api/sync"),
            ("POST", "/api/escalations"),
        ]:
            r = self.client.request(method, path, headers=k_headers)
            self.assertEqual(r.status_code, 403, f"Kiosk should be denied {method} {path}, got {r.status_code}")

    def test_09_xss_escaping_in_executive_report_and_frontend(self):
        payload_xss = "<script>alert('xss')</script>"
        img_xss = '<img src=x onerror=alert("xss2")>'
        fake_report_data = {
            "period_label": f"Periodo {payload_xss}",
            "generated_at": f"13-11-2026 {payload_xss}",
            "filters": {
                "ibp": payload_xss,
                "category": img_xss,
                "specialist": payload_xss,
            },
            "kpis": {
                "total_items": 1,
                "total_pendientes": 1,
                "total_en_curso": 0,
                "total_en_espera": 0,
                "total_cerrados": 0,
                "avg_resolution_str": f"1h {payload_xss}",
                "total_escalamientos": 1,
            },
            "por_especialista": [
                {
                    "name": payload_xss,
                    "role": img_xss,
                    "total": 1,
                    "cerrados": 0,
                    "en_curso": 0,
                    "en_espera": 0,
                    "pendientes": 1,
                    "avg_time_str": img_xss,
                }
            ],
            "por_categoria": [
                {
                    "category": img_xss,
                    "total": 1,
                    "total_items": 1,
                    "total_escalamientos": 1,
                    "cerrados": 0,
                    "en_curso": 0,
                    "en_espera": 0,
                }
            ],
            "por_ibp": [
                {
                    "ibp": payload_xss,
                    "total": 1,
                    "total_items": 1,
                    "total_escalamientos": 1,
                    "cerrados": 0,
                    "en_curso": 0,
                    "en_espera": 0,
                }
            ],
            "por_origen": {"GLPI": 0, "ClickUp": 1},
            "por_prioridad": {"Crítica": 0, "Alta": 0, "Media": 1, "Baja": 0},
            "por_cliente": [
                {
                    "client_name": payload_xss,
                    "client_type": img_xss,
                    "total": 1,
                    "cerrados": 0,
                    "activos": 1,
                    "criticos": 0,
                }
            ],
            "escalations": [
                {
                    "id": 99,
                    "title": payload_xss,
                    "ibp": img_xss,
                    "category": payload_xss,
                    "received_at_str": payload_xss,
                    "escalated_at_str": img_xss,
                    "recommendations": img_xss,
                }
            ],
            "items": [
                {
                    "ibp": payload_xss,
                    "category": img_xss,
                    "status": "pendiente",
                    "escalation_id": None,
                }
            ],
        }
        rendered_html = generate_executive_html(fake_report_data)
        self.assertNotIn("<script>alert('xss')</script>", rendered_html)
        self.assertNotIn("<img src=x onerror=", rendered_html)
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", rendered_html)

        frontend_html = (Path(__file__).resolve().parent.parent / "frontend" / "index.html").read_text(encoding="utf-8")
        for required_marker in (
            "function getSafeExternalUrl(",
            "function getSafeAvatarUrl(",
            "function getSafeToken(",
            "id=\"forcePwOverlay\"",
            "id=\"kioskLoginBox\"",
            "'X-Requested-With': 'XMLHttpRequest'",
            "window.history.replaceState",
            "data-url=",
            "data-spec=",
            "data-cat=",
        ):
            self.assertIn(required_marker, frontend_html, f"Missing security hardening marker in frontend: {required_marker}")

        for forbidden_pattern in ("X-User-Role", "X-User-Name", "X-User-Username"):
            self.assertNotIn(forbidden_pattern, frontend_html, f"Spoofable header still present in frontend: {forbidden_pattern}")


if __name__ == "__main__":
    unittest.main()
