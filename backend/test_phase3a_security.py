import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("backend"))

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main_mod
from app import config
from app.database import Base, get_db
from app.models import User


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


class TestPhase3AHardening(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.orig_override = main_mod.app.dependency_overrides.get(get_db)
        main_mod.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(main_mod.app)
        cls.root_dir = Path(__file__).resolve().parent.parent

    @classmethod
    def tearDownClass(cls):
        if cls.orig_override is None:
            main_mod.app.dependency_overrides.pop(get_db, None)
        else:
            main_mod.app.dependency_overrides[get_db] = cls.orig_override
        test_engine.dispose()

    def setUp(self):
        main_mod._clear_rate_limiters()
        self.orig_cookie_secure = main_mod.SESSION_COOKIE_SECURE
        db = TestingSessionLocal()
        try:
            db.query(User).delete()
            db.add(
                User(
                    full_name="Adonis Lozada",
                    username="a.lozada",
                    email="a.lozada@sycom.com.ve",
                    role="coordinador",
                    password_hash=main_mod.hash_password("Coord$2026!"),
                    is_active=1,
                    must_change_password=0,
                    token_version=1,
                )
            )
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        main_mod.SESSION_COOKIE_SECURE = self.orig_cookie_secure

    def test_01_nginx_security_headers_rate_limits_and_http_compatibility(self):
        nginx_conf = (self.root_dir / "frontend" / "nginx.conf").read_text(encoding="utf-8")
        active_conf = "\n".join(
            line for line in nginx_conf.splitlines() if not line.lstrip().startswith("#")
        )

        # Balanced braces sanity check
        self.assertEqual(nginx_conf.count("{"), nginx_conf.count("}"), "Mismatched braces in frontend/nginx.conf")

        server_pos = active_conf.index("server {")
        pre_server = active_conf[:server_pos]
        for zone_name in ("login_limit:10m", "kiosk_limit:10m", "upload_limit:10m"):
            self.assertIn(f"limit_req_zone $binary_remote_addr zone={zone_name}", pre_server)

        self.assertIn("server_tokens off;", active_conf)
        self.assertIn("limit_req_status 429;", active_conf)
        self.assertIn("error_page 429 = @rate_limited;", active_conf)
        self.assertIn("default_type application/json;", active_conf)

        # Must remain HTTP-compatible until TLS/HTTPS is enabled in Phase 3B
        self.assertNotIn("upgrade-insecure-requests", active_conf)
        self.assertNotIn("Strict-Transport-Security", active_conf)

        # Sensitive endpoints rate-limited
        self.assertRegex(active_conf, r"location\s*=\s*/api/auth/login\s*\{[^}]*limit_req\s+zone=login_limit\s+burst=10\s+nodelay;")
        self.assertRegex(active_conf, r"location\s*=\s*/api/auth/kiosk\s*\{[^}]*limit_req\s+zone=kiosk_limit\s+burst=5\s+nodelay;")
        self.assertRegex(active_conf, r"location\s*=\s*/api/escalations/upload\s*\{[^}]*limit_req\s+zone=upload_limit\s+burst=5\s+nodelay;")

        # Security headers must exist both at server level AND inside location / (Nginx add_header inheritance trap)
        loc_root_match = re.search(r"location\s+/\s*\{(.*?)\n\s*\}", active_conf, re.DOTALL)
        self.assertIsNotNone(loc_root_match)
        loc_root_block = loc_root_match.group(1)
        for hdr in (
            "X-Content-Type-Options \"nosniff\" always;",
            "X-Frame-Options \"SAMEORIGIN\" always;",
            "Referrer-Policy \"strict-origin-when-cross-origin\" always;",
            "Permissions-Policy \"camera=(), microphone=(), geolocation=(), payment=(), usb=()\" always;",
            "Content-Security-Policy \"default-src 'self';",
        ):
            self.assertIn(hdr, active_conf[:loc_root_match.start()], f"Missing server-level header: {hdr}")
            self.assertIn(hdr, loc_root_block, f"Missing replicated header in location /: {hdr}")

        # PDF inline viewer must allow same-origin embedded PDF plugin while SPA keeps object-src 'none'
        self.assertIn("object-src 'none'", loc_root_block)
        self.assertRegex(active_conf, r"location\s+~\s+\^/api/escalations/\[0-9\]\+/view\$\s*\{[^}]*object-src 'self'[^}]*proxy_pass\s+http://backend:8000;")

        # Config and test files in mounted ./frontend must not be served publicly
        self.assertIn("nginx\\.conf|test_deeplink\\.js", active_conf)
        self.assertIn("location ~ /\\.(?!well-known)", active_conf)

    def test_02_dockerfile_non_root_user_and_storage_ownership(self):
        dockerfile = (self.root_dir / "backend" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("--gid 10001", dockerfile)
        self.assertIn("--uid 10001", dockerfile)
        self.assertIn("appuser", dockerfile)
        self.assertIn("COPY --chown=appuser:appuser app/ ./app/", dockerfile)
        self.assertIn("COPY --chown=appuser:appuser migrate_sqlite_to_pg.py .", dockerfile)
        self.assertIn("mkdir -p /app/data/uploads/escalations", dockerfile)
        self.assertIn("chown -R appuser:appuser /app", dockerfile)
        self.assertIn("USER appuser:appuser", dockerfile)
        self.assertLess(
            dockerfile.index("USER appuser:appuser"),
            dockerfile.index("CMD [\"uvicorn\""),
            "USER appuser:appuser must appear before uvicorn CMD",
        )

    def test_03_compose_hardening_volume_separation_and_healthcheck(self):
        compose = (self.root_dir / "docker-compose.yml").read_text(encoding="utf-8")
        # Phase 1 port exposure constraints remain intact
        self.assertIn("expose:", compose)
        self.assertNotIn("8000:8000", compose)

        # Production container runs immutable image without editable code bind mount or root volume shadowing /app
        self.assertNotIn("./backend/app:/app/app", compose)
        self.assertNotIn("./backend/migrate_sqlite_to_pg.py:/app/migrate_sqlite_to_pg.py", compose)
        self.assertNotIn("dashboard_data:/app/\n", compose)
        self.assertIn("dashboard_data:/app/data", compose)
        self.assertIn("UPLOAD_ESCALATIONS_DIR=/app/data/uploads/escalations", compose)

        # Kernel capabilities & privilege escalation restrictions + healthcheck dependency
        self.assertIn("cap_drop:", compose)
        self.assertIn("- ALL", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("curl -fsS http://localhost:8000/api/health", compose)
        self.assertIn("condition: service_healthy", compose)

    def test_04_upload_dir_resolution_and_path_traversal_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_pkg = Path(tmp) / "app"
            app_pkg.mkdir(parents=True)
            fake_main = app_pkg / "main.py"
            fake_main.write_text("# dummy", encoding="utf-8")

            # Case 1: explicit env override wins
            with patch.dict(os.environ, {"UPLOAD_ESCALATIONS_DIR": os.path.join(tmp, "custom_pdfs")}):
                self.assertEqual(
                    Path(main_mod._resolve_default_upload_dir()).resolve(),
                    (Path(tmp) / "custom_pdfs").resolve(),
                )

            # Case 2: /app/data exists -> uses <root>/data/uploads/escalations
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("UPLOAD_ESCALATIONS_DIR", None)
                with patch.object(main_mod, "__file__", str(fake_main)):
                    self.assertEqual(
                        Path(main_mod._resolve_default_upload_dir()).resolve(),
                        (data_dir / "uploads" / "escalations").resolve(),
                    )

            # Case 3: no data dir -> local dev fallback <root>/uploads/escalations
            data_dir.rmdir()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("UPLOAD_ESCALATIONS_DIR", None)
                with patch.object(main_mod, "__file__", str(fake_main)):
                    self.assertEqual(
                        Path(main_mod._resolve_default_upload_dir()).resolve(),
                        (Path(tmp) / "uploads" / "escalations").resolve(),
                    )

        # Traversal remains blocked
        with self.assertRaises(HTTPException) as ctx:
            main_mod._resolve_safe_escalation_path("../secret.pdf")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_05_unauthenticated_healthcheck_and_plain_http_session_cookie(self):
        h = self.client.get("/api/health")
        self.assertEqual(h.status_code, 200)
        self.assertEqual(h.json().get("status"), "ok")

        # While running HTTP (SESSION_COOKIE_SECURE=False), session_token must NOT carry Secure flag
        main_mod.SESSION_COOKIE_SECURE = False
        r = self.client.post("/api/auth/login", json={"username": "a.lozada", "password": "Coord$2026!"})
        self.assertEqual(r.status_code, 200)
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("session_token=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        self.assertNotIn("; Secure", set_cookie)


if __name__ == "__main__":
    unittest.main()
