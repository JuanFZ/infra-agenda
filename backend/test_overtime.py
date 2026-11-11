import os
import sys
import datetime
import urllib.parse
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Setup test environment
sys.path.insert(0, os.path.dirname(__file__))
from app.database import Base, get_db
from app.models import User, OvertimeRecord
from app.main import app, ensure_db_schema

# Use in-memory SQLite database with StaticPool for test continuity across threads
TEST_DB_URL = "sqlite:///:memory:"
engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

def setup_module(module):
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    # Create test users
    u_coord = User(full_name="Adonis Lozada", username="a.lozada", email="a.lozada@sycom.com.ve", role="coordinador")
    u_spec1 = User(full_name="Juan Fernández", username="j.fernandez", email="j.fernandez@sycom.com.ve", role="especialista")
    u_spec2 = User(full_name="Yoxaide Jiménez", username="y.jimenez", email="y.jimenez@sycom.com.ve", role="especialista")
    db.add_all([u_coord, u_spec1, u_spec2])
    db.commit()
    db.close()

def test_01_create_overtime_and_duration_calculation():
    payload = {
        "start_time": "2026-11-12T18:00:00",
        "end_time": "2026-11-12T20:30:00",
        "description": "Atención de contingencia en base de datos PostgreSQL",
        "category": "PostgreSQL",
        "project_name": "SIMF"
    }
    headers = {
        "X-User-Role": "Especialista",
        "X-User-Name": urllib.parse.quote("Juan Fernández")
    }
    res = client.post("/api/overtimes", json=payload, headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["specialist_name"] == "Juan Fernández"
    assert data["duration_minutes"] == 150
    assert data["duration_hours"] == 2.5
    assert data["duration_human"] == "2h 30m"
    assert data["category"] == "PostgreSQL"
    assert data["project_name"] == "SIMF"
    assert data["can_edit"] is True
    assert data["can_delete"] is True

def test_02_create_overtime_invalid_range():
    payload = {
        "start_time": "2026-11-12T21:00:00",
        "end_time": "2026-11-12T20:00:00",
        "description": "Rango inválido",
        "category": "Infraestructura"
    }
    headers = {"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Juan Fernández")}
    res = client.post("/api/overtimes", json=payload, headers=headers)
    assert res.status_code == 400
    assert "posterior al inicio" in res.json()["detail"]

def test_03_specialist_cannot_create_for_another():
    payload = {
        "specialist_name": "Yoxaide Jiménez",
        "start_time": "2026-11-12T19:00:00",
        "end_time": "2026-11-12T21:00:00",
        "description": "Guardia de soporte técnico nocturno",
        "category": "Servidores"
    }
    headers = {"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Juan Fernández")}
    res = client.post("/api/overtimes", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    # Must enforce current specialist's name
    assert data["specialist_name"] == "Juan Fernández"

def test_04_coordinator_can_create_for_anyone():
    payload = {
        "specialist_name": "Yoxaide Jiménez",
        "start_time": "2026-11-12T19:00:00",
        "end_time": "2026-11-12T21:00:00",
        "description": "Mantenimiento preventivo servidores",
        "category": "Infraestructura"
    }
    headers = {"X-User-Role": "Coordinador", "X-User-Name": urllib.parse.quote("Adonis Lozada")}
    res = client.post("/api/overtimes", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["specialist_name"] == "Yoxaide Jiménez"
    assert data["created_by"] == "Adonis Lozada"
    assert data["created_by_role"] == "Coordinador"

def test_05_permissions_on_delete():
    # Record created by Juan Fernández
    payload = {
        "start_time": "2026-11-13T18:00:00",
        "end_time": "2026-11-13T19:00:00",
        "description": "Prueba de permisos para borrado",
        "category": "Infraestructura"
    }
    res = client.post("/api/overtimes", json=payload, headers={"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Juan Fernández")})
    rec_id = res.json()["id"]

    # Yoxaide Jiménez tries to delete Juan's record -> 403 Forbidden
    res_del_forbidden = client.delete(f"/api/overtimes/{rec_id}", headers={"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Yoxaide Jiménez")})
    assert res_del_forbidden.status_code == 403

    # Coordinator can delete it -> 200 OK
    res_del_ok = client.delete(f"/api/overtimes/{rec_id}", headers={"X-User-Role": "Coordinador", "X-User-Name": urllib.parse.quote("Adonis Lozada")})
    assert res_del_ok.status_code == 200

def test_06_analytics_and_heatmap_matrix():
    res = client.get("/api/overtimes/analytics")
    print("ANALYTICS RESULT:", res.status_code, res.text)
    assert res.status_code == 200
    data = res.json()
    assert "total_records" in data
    assert "total_hours" in data
    assert "by_specialist" in data
    assert "heatmap_matrix" in data

    hm = data["heatmap_matrix"]
    assert "specialists" in hm
    assert "categories" in hm
    assert "cells" in hm
    assert "row_totals" in hm
    assert "col_totals" in hm

def test_07_zero_telegram_calls_on_overtime():
    with patch("app.notifications.telegram_notifier.send_telegram_message") as mock_tg:
        payload = {
            "start_time": "2026-11-14T18:00:00",
            "end_time": "2026-11-14T20:00:00",
            "description": "Auditoría de seguridad y políticas de conexión",
            "category": "PostgreSQL"
        }
        res = client.post("/api/overtimes", json=payload, headers={"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Juan Fernández")})
        assert res.status_code == 200
        rec_id = res.json()["id"]

        client.put(f"/api/overtimes/{rec_id}", json={"description": "Actualización de auditoría"}, headers={"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Juan Fernández")})
        client.delete(f"/api/overtimes/{rec_id}", headers={"X-User-Role": "Especialista", "X-User-Name": urllib.parse.quote("Juan Fernández")})

        # ZERO Telegram calls must be made
        mock_tg.assert_not_called()

if __name__ == "__main__":
    setup_module(None)
    tests = [
        test_01_create_overtime_and_duration_calculation,
        test_02_create_overtime_invalid_range,
        test_03_specialist_cannot_create_for_another,
        test_04_coordinator_can_create_for_anyone,
        test_05_permissions_on_delete,
        test_06_analytics_and_heatmap_matrix,
        test_07_zero_telegram_calls_on_overtime
    ]
    for t in tests:
        t()
        print(f"PASS: {t.__name__}")
    print("ALL OVERTIME BACKEND TESTS PASSED SUCCESSFULLY!")
