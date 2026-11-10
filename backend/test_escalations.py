import os
import sys
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0, os.path.abspath('backend'))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import Item, EscalationDocument, User, IBP
from app.main import app, VALID_ESCALATION_CATEGORIES, ensure_db_schema, IBP_INVENTORY, normalize_ibp

test_engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db

class TestEscalationFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=test_engine)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        test_engine.dispose()

    def setUp(self):
        db = TestingSessionLocal()
        try:
            db.query(Item).delete()
            db.query(EscalationDocument).delete()
            db.commit()
        finally:
            db.close()

    def test_01_categories_validation(self):
        expected_cats = ["CPU", "RAM", "PostgreSQL", "Servicios BD", "Discos", "Procesos"]
        self.assertEqual(VALID_ESCALATION_CATEGORIES, expected_cats)

        # Test creating escalation with each category
        for cat in expected_cats:
            res = self.client.post("/api/escalations", json={
                "title": f"Escalamiento {cat}",
                "category": cat,
                "doc_url": f"https://docs.google.com/document/d/{cat}",
                "notes": f"Notas para {cat}",
                "recommendations": f"Recs iniciales para {cat}"
            }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
            self.assertEqual(res.status_code, 200)
            data = res.json()["document"]
            self.assertEqual(data["category"], cat)
            self.assertEqual(data["recommendations"], f"Recs iniciales para {cat}")

        # Test fallback category for invalid input
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Categoria Invalida",
            "category": "CualquierCosa",
            "doc_url": "https://docs.google.com/document/d/xyz"
        })
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["document"]["category"], "Servicios BD")

        # Test legacy category normalization ("Servicios" -> "Servicios BD", "Disco" -> "Discos")
        res_serv = self.client.post("/api/escalations", json={
            "title": "Escalamiento Legacy Servicios",
            "category": "Servicios",
            "doc_url": "https://docs.google.com/document/d/serv1"
        })
        self.assertEqual(res_serv.status_code, 200)
        self.assertEqual(res_serv.json()["document"]["category"], "Servicios BD")

        res_disc = self.client.post("/api/escalations", json={
            "title": "Escalamiento Legacy Disco",
            "category": "Disco",
            "doc_url": "https://docs.google.com/document/d/disc1"
        })
        self.assertEqual(res_disc.status_code, 200)
        self.assertEqual(res_disc.json()["document"]["category"], "Discos")

    def test_02_update_escalation(self):
        # Create an escalation
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Base",
            "category": "RAM",
            "doc_url": "https://docs.google.com/document/d/base",
            "notes": "Notas base",
            "recommendations": "Recs base"
        })
        esc_id = res.json()["document"]["id"]

        # Update category, title, notes, recommendations, url
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "title": "Escalamiento Editado",
            "category": "PostgreSQL",
            "doc_url": "https://docs.google.com/document/d/edited",
            "notes": "Notas actualizadas",
            "recommendations": "Recs actualizadas"
        })
        self.assertEqual(put_res.status_code, 200)
        updated = put_res.json()["document"]
        self.assertEqual(updated["title"], "Escalamiento Editado")
        self.assertEqual(updated["category"], "PostgreSQL")

        # Verify through GET
        get_res = self.client.get("/api/escalations")
        docs = get_res.json()
        doc = next(d for d in docs if d["id"] == esc_id)
        self.assertEqual(doc["title"], "Escalamiento Editado")
        self.assertEqual(doc["category"], "PostgreSQL")
        self.assertEqual(doc["recommendations"], "Recs actualizadas")

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    def test_03_create_item_with_escalation_link_and_description(self, mock_cu):
        mock_cu.return_value = {"id": "cu123", "url": "https://app.clickup.com/t/cu123"}

        # Create escalation first
        esc_res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Para Item",
            "category": "CPU",
            "doc_url": "https://docs.google.com/document/d/item1"
        })
        esc_id = esc_res.json()["document"]["id"]

        # Create item linked to escalation with description
        item_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Actividad vinculada a CPU",
            "description": "Detalle técnico de la actividad",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(item_res.status_code, 200)
        item_data = item_res.json()["item"]
        self.assertEqual(item_data["escalation_id"], esc_id)

        # Verify through database and GET /api/items
        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_data["id"]).first()
        self.assertEqual(item_db.description, "Detalle técnico de la actividad")
        self.assertEqual(item_db.escalation_id, esc_id)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_04_close_item_with_recommendations_and_escalation(self, mock_cu):
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc = EscalationDocument(
            title="Escalamiento Cierre",
            category="Disco",
            doc_url="https://docs.google.com/document/d/disco1",
            recommendations=None
        )
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu999",
            title="Optimizar discos del servidor",
            status="curso",
            escalation_id=esc_id,
            assignee_name="Juan Fernández"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Close item as assigned specialist with recommendations (using URL-encoded header for non-ascii name)
        spec_name_encoded = urllib.parse.quote("Juan Fernández")
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Se liberaron 50GB purgando logs antiguos.",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "especialista", "X-User-Name": spec_name_encoded})
        self.assertEqual(close_res.status_code, 200)

        # Check item has recommendations
        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_id).first()
        self.assertEqual(item_db.status, "cerrado")
        self.assertEqual(item_db.recommendations, "Se liberaron 50GB purgando logs antiguos.")

        # Check escalation document has recommendations appended
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Se liberaron 50GB purgando logs antiguos.", esc_db.recommendations)
        self.assertIn("ClickUp #cu999", esc_db.recommendations)

        # Close again: should not create duplicate
        close_again = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Se liberaron 50GB purgando logs antiguos.",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "especialista", "X-User-Name": spec_name_encoded})
        self.assertEqual(close_again.status_code, 200)
        db.refresh(esc_db)
        # Verify it occurs only once
        self.assertEqual(esc_db.recommendations.count("Se liberaron 50GB purgando logs antiguos."), 1)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_05_close_item_unlinking_or_changing_escalation(self, mock_cu):
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc1 = EscalationDocument(title="Escalamiento 1", category="RAM", doc_url="https://docs.google.com/1")
        esc2 = EscalationDocument(title="Escalamiento 2", category="PostgreSQL", doc_url="https://docs.google.com/2")
        db.add_all([esc1, esc2])
        db.commit()
        esc1_id, esc2_id = esc1.id, esc2.id

        # Item initially linked to esc1
        item = Item(source="ClickUp", external_id="cu888", title="Tarea BD", status="curso", escalation_id=esc1_id, assignee_name="Adonis Lozada")
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Close item, changing escalation to esc2 as coordinator
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Recs para esc2",
            "escalation_id": esc2_id
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res.status_code, 200)

        db = TestingSessionLocal()
        esc1_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc1_id).first()
        esc2_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc2_id).first()
        item_db = db.query(Item).filter(Item.id == item_id).first()

        self.assertEqual(item_db.escalation_id, esc2_id)
        self.assertIsNone(esc1_db.recommendations)
        self.assertIsNotNone(esc2_db.recommendations)
        self.assertIn("Recs para esc2", esc2_db.recommendations)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_06_close_item_unlinking_to_none(self, mock_cu):
        """EDGE CASE: Item was linked to esc1, but user closes it with escalation_id=0 (unlinked)."""
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc1 = EscalationDocument(title="Escalamiento Unlink", category="Servicios", doc_url="https://docs.google.com/av")
        db.add(esc1)
        db.commit()
        esc1_id = esc1.id

        item = Item(source="ClickUp", external_id="cu777", title="Tarea AV", status="curso", escalation_id=esc1_id, assignee_name="Adonis Lozada")
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # User unlinks escalation upon closing: sends escalation_id=0
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Recomendacion suelta sin escalamiento",
            "escalation_id": 0
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res.status_code, 200)

        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_id).first()
        esc1_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc1_id).first()

        self.assertIsNone(item_db.escalation_id)
        self.assertIsNone(esc1_db.recommendations)
        db.close()

    def test_07_specialist_and_coordinator_edition_via_patch(self):
        """Test that specialists can link/unlink escalation and write recommendations via PATCH /api/items/{id}."""
        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento Parche", category="Servicios", doc_url="https://docs.google.com/serv")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu666",
            title="Tarea Parche Especialista",
            status="curso",
            assignee_name="Yoxaide Jiménez",
            priority="Media"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        spec_header = {
            "X-User-Role": "especialista",
            "X-User-Name": urllib.parse.quote("Yoxaide Jiménez")
        }

        # Specialist links item to escalation and adds recommendations
        patch_res = self.client.patch(f"/api/items/{item_id}", json={
            "escalation_id": esc_id,
            "recommendations": "Recomendación preventiva añadida por Yoxaide"
        }, headers=spec_header)
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["item"]["escalation_id"], esc_id)

        # Specialist attempts to modify priority -> 403 Forbidden
        denied_res = self.client.patch(f"/api/items/{item_id}", json={
            "priority": "Crítica"
        }, headers=spec_header)
        self.assertEqual(denied_res.status_code, 403)

        # Non-assigned specialist attempts to edit -> 403 Forbidden
        other_spec = {
            "X-User-Role": "especialista",
            "X-User-Name": "Luis Acosta"
        }
        denied_res2 = self.client.patch(f"/api/items/{item_id}", json={
            "recommendations": "Intrusión"
        }, headers=other_spec)
        self.assertEqual(denied_res2.status_code, 403)

        # Coordinator can modify anything
        coord_res = self.client.patch(f"/api/items/{item_id}", json={
            "priority": "Alta",
            "description": "Nueva descripcion por coordinador"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(coord_res.status_code, 200)
        self.assertEqual(coord_res.json()["item"]["priority"], "Alta")

        # Verify escalation recommendations were updated
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIn("Recomendación preventiva añadida por Yoxaide", esc_db.recommendations)
        db.close()

    def test_08_ibp_catalog_and_escalation_linkage(self):
        """Test IBP catalog management and linking escalations to IBPs."""
        db = TestingSessionLocal()
        # Ensure at least standard IBPs exist in test db
        if db.query(IBP).count() == 0:
            db.add_all([
                IBP(name="Banesco", code="BANESCO", description="Instancia Banesco"),
                IBP(name="Banco Mercantil", code="MERCANTIL", description="Instancia Mercantil")
            ])
            db.commit()
        db.close()

        # 1. GET /api/ibps
        get_ibps_res = self.client.get("/api/ibps")
        self.assertEqual(get_ibps_res.status_code, 200)
        ibps = get_ibps_res.json()
        self.assertGreater(len(ibps), 0)
        banesco = next((i for i in ibps if i.get("code") in ("0134", "BANESCO") or "BANESCO" in (i.get("name") or "").upper()), None)
        self.assertIsNotNone(banesco)

        # 2. POST /api/ibps - Coordinator creates new IBP
        create_ibp_res = self.client.post("/api/ibps", json={
            "name": "Banco Plaza",
            "code": "PLAZA",
            "description": "Instancia Banco Plaza C.A."
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(create_ibp_res.status_code, 200)
        created_ibp = create_ibp_res.json()["ibp"]
        self.assertEqual(created_ibp["name"], "Banco Plaza")
        self.assertEqual(created_ibp["code"], "PLAZA")
        plaza_id = created_ibp["id"]

        # Duplicate check -> 400
        dup_res = self.client.post("/api/ibps", json={
            "name": "Banco Plaza"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(dup_res.status_code, 400)

        # 3. Create Escalation linked to IBP
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-8899 Banco Plaza Escalation",
            "category": "PostgreSQL",
            "ibp_id": plaza_id,
            "doc_url": "https://docs.google.com/document/d/plaza8899",
            "notes": "Escalamiento en servidor srvbdplaza",
            "recommendations": "Revisar max_connections"
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(esc_res.status_code, 200)
        esc_doc = esc_res.json()["document"]
        self.assertEqual(esc_doc["ibp_id"], plaza_id)
        self.assertEqual(esc_doc["ibp_name"], "Banco Plaza")
        self.assertEqual(esc_doc["ibp_code"], "PLAZA")
        esc_id = esc_doc["id"]

        # 4. Verify in GET /api/escalations
        list_res = self.client.get("/api/escalations")
        self.assertEqual(list_res.status_code, 200)
        found = next(d for d in list_res.json() if d["id"] == esc_id)
        self.assertEqual(found["ibp_id"], plaza_id)
        self.assertEqual(found["ibp_name"], "Banco Plaza")

        # 5. Update Escalation changing IBP to Banesco
        banesco_id = banesco["id"]
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp_id": banesco_id,
            "category": "CPU"
        })
        self.assertEqual(put_res.status_code, 200)
        updated_doc = put_res.json()["document"]
        self.assertEqual(updated_doc["ibp_id"], banesco_id)
        self.assertEqual(updated_doc["ibp_name"], banesco["name"])
        self.assertIn("BANESCO", updated_doc["ibp_name"].upper())
        self.assertEqual(updated_doc["category"], "CPU")

        # 6. Unlink IBP (set ibp_id = 0)
        unlink_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp_id": 0
        })
        self.assertEqual(unlink_res.status_code, 200)
        unlinked_doc = unlink_res.json()["document"]
        self.assertIsNone(unlinked_doc["ibp_id"])
        self.assertIsNone(unlinked_doc["ibp_name"])

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    @patch("app.sync.glpi_client.GLPIClient.create_ticket")
    def test_09_create_item_with_platform_subprojects(self, mock_glpi, mock_cu):
        """Test creating activities with project selector (SIMF, SGLPAR) for ClickUp and (Sycom, MicroFilms Center) for GLPI."""
        mock_cu.return_value = {"id": "cu_sub1", "url": "https://app.clickup.com/t/cu_sub1"}
        mock_glpi.return_value = {"id": "glpi_sub2", "url": "https://glpi.sycom.com.ve/ticket/glpi_sub2"}

        # 1. ClickUp with SIMF
        cu_res1 = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Optimización SIMF motor de pagos",
            "project_name": "SIMF"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(cu_res1.status_code, 200)
        item1 = cu_res1.json()["item"]
        self.assertEqual(item1["client_name"], "SIMF")
        self.assertEqual(item1["project_name"], "SIMF")

        # 2. ClickUp with SGLPAR
        mock_cu.return_value = {"id": "cu_sub2", "url": "https://app.clickup.com/t/cu_sub2"}
        cu_res2 = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Ajuste SGLPAR conciliaciones",
            "project_name": "SGLPAR"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(cu_res2.status_code, 200)
        item2 = cu_res2.json()["item"]
        self.assertEqual(item2["client_name"], "SGLPAR")

        # 3. GLPI with Sycom
        glpi_res1 = self.client.post("/api/items", json={
            "source": "GLPI",
            "title": "Ticket Soporte Servidores Sycom",
            "client_name": "Sycom"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(glpi_res1.status_code, 200)
        item3 = glpi_res1.json()["item"]
        self.assertEqual(item3["client_name"], "Sycom")

        # 4. GLPI with MicroFilms Center
        mock_glpi.return_value = {"id": "glpi_sub4", "url": "https://glpi.sycom.com.ve/ticket/glpi_sub4"}
        glpi_res2 = self.client.post("/api/items", json={
            "source": "GLPI",
            "title": "Ticket Escaneo MicroFilms Center",
            "client_name": "MicroFilms Center"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(glpi_res2.status_code, 200)
        item4 = glpi_res2.json()["item"]
        self.assertEqual(item4["client_name"], "MicroFilms Center")

        # 5. Coordinator updates project_name via PATCH
        patch_res = self.client.patch(f"/api/items/{item1['id']}", json={
            "project_name": "SIMF-Core"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["item"]["project_name"], "SIMF-Core")
        self.assertEqual(patch_res.json()["item"]["client_name"], "SIMF-Core")

        # 6. Verify GET /api/items exposes project_name and client_name
        get_items_res = self.client.get("/api/items")
        self.assertEqual(get_items_res.status_code, 200)
        all_it = get_items_res.json()
        f_it1 = next(i for i in all_it if i["id"] == item1["id"])
        self.assertEqual(f_it1["project_name"], "SIMF-Core")
        self.assertEqual(f_it1["client_name"], "SIMF-Core")

    @patch("app.sync.clickup_client.ClickUpClient.create_task")
    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_10_edge_cases_and_robustness(self, mock_cu_status, mock_cu_create):
        """Test edge cases: non-existent ibp_id, whitespace recommendations, default subprojects."""
        mock_cu_create.return_value = {"id": "cu_edge1", "url": "https://app.clickup.com/t/cu_edge1"}
        mock_cu_status.return_value = True

        # Non-existent IBP ID
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-9999 Invalid IBP",
            "category": "Procesos",
            "ibp_id": 999999,
            "doc_url": "https://docs.google.com/document/d/edge1"
        })
        self.assertEqual(esc_res.status_code, 200)
        self.assertIsNone(esc_res.json()["document"]["ibp_id"])
        esc_id = esc_res.json()["document"]["id"]

        # Default subprojects when none provided
        item_def_res = self.client.post("/api/items", json={
            "source": "ClickUp",
            "title": "Tarea sin proyecto explícito",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(item_def_res.status_code, 200)
        self.assertEqual(item_def_res.json()["item"]["project_name"], "SIMF")
        item_id = item_def_res.json()["item"]["id"]

        # Close with whitespace recommendations -> should not append empty string
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "   \n\t   ",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res.status_code, 200)

        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNone(esc_db.recommendations)

        # Close again with real recommendations -> appends properly
        close_res2 = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado",
            "recommendations": "Solución técnica aplicada correctamente.",
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador", "X-User-Name": "Adonis Lozada"})
        self.assertEqual(close_res2.status_code, 200)

        db.refresh(esc_db)
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Solución técnica aplicada correctamente.", esc_db.recommendations)
        db.close()

    def test_11_ibp_string_persistence_and_official_catalog(self):
        """Test official 34 IBP catalog, string normalization, and direct database persistence on EscalationDocument."""
        # 1. Verify 34 official IBPs in inventory
        self.assertEqual(len(IBP_INVENTORY), 34)
        expected_codes = [
            "0000", "0001", "0102", "0104", "0105", "0108", "0114", "0115",
            "0128", "0134", "0137", "0138", "0145", "0146", "0151", "0152",
            "0156", "0157", "0163", "0166", "0168", "0169", "0171", "0172",
            "0173", "0174", "0175", "0177", "0178", "0191", "0601", "0882",
            "0883", "2017"
        ]
        actual_codes = [item["code"] for item in IBP_INVENTORY]
        self.assertEqual(actual_codes, expected_codes)

        # 2. Test normalization helper across all codes, aliases, accents and edge cases
        self.assertEqual(normalize_ibp("0134 - BANESCO"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("0134"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("134"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("1"), "0001 - BCV")
        self.assertEqual(normalize_ibp("BANESCO"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("banesco"), "0134 - BANESCO")
        self.assertEqual(normalize_ibp("0102"), "0102 - BDV")
        self.assertEqual(normalize_ibp("BDV"), "0102 - BDV")
        self.assertEqual(normalize_ibp("Banco de Venezuela"), "0102 - BDV")
        self.assertEqual(normalize_ibp("Venezuela"), "0102 - BDV")
        self.assertEqual(normalize_ibp("MERCANTIL"), "0105 - MERCANTIL")
        self.assertEqual(normalize_ibp("BBVA"), "0108 - PROVINCIAL")
        self.assertEqual(normalize_ibp("BBVA Provincial"), "0108 - PROVINCIAL")
        self.assertEqual(normalize_ibp("Caroni"), "0128 - CARONÍ")
        self.assertEqual(normalize_ibp("Caroní"), "0128 - CARONÍ")
        self.assertEqual(normalize_ibp("Agricola"), "0166 - AGRÍCOLA")
        self.assertEqual(normalize_ibp("Agrícola"), "0166 - AGRÍCOLA")
        self.assertEqual(normalize_ibp("100% Banco"), "0156 - 100%BANCO")
        self.assertEqual(normalize_ibp("100%BANCO"), "0156 - 100%BANCO")
        self.assertEqual(normalize_ibp("Bicentenario"), "0175 - BDT")
        self.assertEqual(normalize_ibp("BDT"), "0175 - BDT")
        self.assertEqual(normalize_ibp("0882"), "0882 - Suiche7B (Consorcio)")
        self.assertEqual(normalize_ibp("Suiche7B"), "0882 - Suiche7B (Consorcio)")
        self.assertEqual(normalize_ibp("Suiche 7B"), "0882 - Suiche7B (Consorcio)")
        self.assertEqual(normalize_ibp("Conexus"), "0883 - CONEXUS (Consorcio)")
        self.assertEqual(normalize_ibp("0883"), "0883 - CONEXUS (Consorcio)")
        self.assertEqual(normalize_ibp("Banco Central de Venezuela"), "0001 - BCV")
        self.assertIsNone(normalize_ibp(""))
        self.assertIsNone(normalize_ibp(None))

        # 3. Create Escalation with explicit IBP string
        res = self.client.post("/api/escalations", json={
            "title": "Escalamiento Banesco SIMF-0134",
            "category": "PostgreSQL",
            "ibp": "0134 - BANESCO",
            "doc_url": "https://docs.google.com/document/d/banesco0134"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(res.status_code, 200)
        doc_data = res.json()["document"]
        self.assertEqual(doc_data["ibp"], "0134 - BANESCO")
        esc_id = doc_data["id"]

        # 4. Verify in DB directly: EscalationDocument.ibp column
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db)
        self.assertEqual(esc_db.ibp, "0134 - BANESCO")
        db.close()

        # 5. Create Escalation with code only -> should normalize to label
        res_norm = self.client.post("/api/escalations", json={
            "title": "Escalamiento BDV SIMF-0102",
            "category": "CPU",
            "ibp": "0102",
            "doc_url": "https://docs.google.com/document/d/bdv0102"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(res_norm.status_code, 200)
        self.assertEqual(res_norm.json()["document"]["ibp"], "0102 - BDV")
        esc_bdv_id = res_norm.json()["document"]["id"]

        db = TestingSessionLocal()
        esc_bdv_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_bdv_id).first()
        self.assertEqual(esc_bdv_db.ibp, "0102 - BDV")
        db.close()

        # 6. Update Escalation changing IBP
        put_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": "0108 - PROVINCIAL"
        })
        self.assertEqual(put_res.status_code, 200)
        self.assertEqual(put_res.json()["document"]["ibp"], "0108 - PROVINCIAL")

        db = TestingSessionLocal()
        esc_updated = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertEqual(esc_updated.ibp, "0108 - PROVINCIAL")
        db.close()

        # 7. Unlink IBP via exact frontend modal payload: {"ibp": None, "ibp_id": None}
        unlink_null_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": None,
            "ibp_id": None
        })
        self.assertEqual(unlink_null_res.status_code, 200)
        self.assertEqual(unlink_null_res.json()["document"]["ibp"], "")
        self.assertIsNone(unlink_null_res.json()["document"]["ibp_id"])

        db = TestingSessionLocal()
        esc_unlinked_null = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNone(esc_unlinked_null.ibp)
        self.assertIsNone(esc_unlinked_null.ibp_id)
        db.close()

        # 8. Re-link and then unlink via empty string
        relink_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": "0134 - BANESCO"
        })
        self.assertEqual(relink_res.status_code, 200)
        self.assertEqual(relink_res.json()["document"]["ibp"], "0134 - BANESCO")

        unlink_empty_res = self.client.put(f"/api/escalations/{esc_id}", json={
            "ibp": ""
        })
        self.assertEqual(unlink_empty_res.status_code, 200)
        self.assertEqual(unlink_empty_res.json()["document"]["ibp"], "")

        db = TestingSessionLocal()
        esc_unlinked = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNone(esc_unlinked.ibp)
        self.assertIsNone(esc_unlinked.ibp_id)
        db.close()

        # 9. Verify GET /api/escalations contains ibp field in all records
        get_res = self.client.get("/api/escalations")
        self.assertEqual(get_res.status_code, 200)
        escalations = get_res.json()
        bdv_item = next(e for e in escalations if e["id"] == esc_bdv_id)
        self.assertEqual(bdv_item["ibp"], "0102 - BDV")

        # 10. Seed official catalog into test DB and verify GET /api/ibps
        db = TestingSessionLocal()
        db.query(IBP).delete()
        for item in IBP_INVENTORY:
            db.add(IBP(name=item["label"], code=item["code"], is_active=1))
        db.commit()
        db.close()

        get_ibps_res = self.client.get("/api/ibps")
        self.assertEqual(get_ibps_res.status_code, 200)
        ibps_list = get_ibps_res.json()
        self.assertEqual(len(ibps_list), 34)
        self.assertEqual([x["code"] for x in ibps_list], expected_codes)

    def test_12_recommendations_sync_when_linking_existing_item(self):
        """Test that when an item already has recommendations and is linked to an escalation via PATCH,
        its recommendations are automatically appended to the escalation document."""
        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento Sincronizar", category="PostgreSQL", doc_url="https://docs.google.com/sync1")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu_sync1",
            title="Actividad con recomendación previa",
            status="curso",
            recommendations="Recomendación técnica documentada con antelación.",
            escalation_id=None,
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Link to escalation via PATCH without resending recommendations
        patch_res = self.client.patch(f"/api/items/{item_id}", json={
            "escalation_id": esc_id
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(patch_res.status_code, 200)

        # Verify escalation document received the recommendations
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Recomendación técnica documentada con antelación.", esc_db.recommendations)
        self.assertIn("ClickUp #cu_sync1", esc_db.recommendations)
        db.close()

    @patch("app.sync.clickup_client.ClickUpClient.update_task_status")
    def test_13_close_item_with_existing_recommendations_without_resending(self, mock_cu):
        """Test that closing an item that already has recommendations appends them to the escalation
        even if the status update request does not explicitly pass recommendations in the body."""
        mock_cu.return_value = True

        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento Cierre Directo", category="Servicios", doc_url="https://docs.google.com/close1")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu_close1",
            title="Actividad para cerrar directo",
            status="curso",
            recommendations="Causa raíz identificada y solventada en BD.",
            escalation_id=esc_id,
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Close without passing recommendations in the body
        close_res = self.client.post(f"/api/items/{item_id}/status", json={
            "status": "cerrado"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(close_res.status_code, 200)

        # Verify escalation document received the recommendations
        db = TestingSessionLocal()
        esc_db = db.query(EscalationDocument).filter(EscalationDocument.id == esc_id).first()
        self.assertIsNotNone(esc_db.recommendations)
        self.assertIn("Causa raíz identificada y solventada en BD.", esc_db.recommendations)
        db.close()

    def test_14_delete_escalation_unlinks_items(self):
        """Test that deleting an escalation safely unlinks any items that referenced it."""
        db = TestingSessionLocal()
        esc = EscalationDocument(title="Escalamiento a Eliminar", category="CPU", doc_url="https://docs.google.com/del1")
        db.add(esc)
        db.commit()
        esc_id = esc.id

        item = Item(
            source="ClickUp",
            external_id="cu_del1",
            title="Actividad vinculada a escalamiento transitorio",
            status="curso",
            escalation_id=esc_id,
            assignee_name="Adonis Lozada"
        )
        db.add(item)
        db.commit()
        item_id = item.id
        db.close()

        # Delete escalation
        del_res = self.client.delete(f"/api/escalations/{esc_id}", headers={"X-User-Role": "Coordinador"})
        self.assertEqual(del_res.status_code, 200)

        # Check item escalation_id is None
        db = TestingSessionLocal()
        item_db = db.query(Item).filter(Item.id == item_id).first()
        self.assertIsNone(item_db.escalation_id)
        db.close()

    def test_15_frontend_category_and_ibp_consistency(self):
        """Verify that categories and IBP structures in frontend/index.html match backend specs."""
        expected_cats = ["CPU", "RAM", "PostgreSQL", "Servicios BD", "Discos", "Procesos"]
        self.assertEqual(VALID_ESCALATION_CATEGORIES, expected_cats)

        # Check frontend/index.html content
        frontend_path = os.path.abspath("frontend/index.html")
        self.assertTrue(os.path.isfile(frontend_path))
        with open(frontend_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Categories list in JS
        self.assertIn("const ESCALATION_CATEGORIES = ['CPU', 'RAM', 'PostgreSQL', 'Servicios BD', 'Discos', 'Procesos'];", html)

        # Category filter dropdown options
        for cat in VALID_ESCALATION_CATEGORIES:
            self.assertIn(f'<option value="{cat}">{cat}</option>', html)

        # 34 IBPs in IBP_INVENTORY
        self.assertEqual(len(IBP_INVENTORY), 34)

    def test_16_custom_ibp_creation_and_escalation_linkage(self):
        """Test creating a custom IBP and linking an escalation to it."""
        res_create = self.client.post("/api/ibps", json={
            "name": "Banco Central de Integración",
            "code": "BCI",
            "description": "Entidad bancaria internacional"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(res_create.status_code, 200)
        custom_ibp = res_create.json()["ibp"]
        self.assertEqual(custom_ibp["code"], "BCI")

        # Create escalation with this custom IBP ID
        esc_res = self.client.post("/api/escalations", json={
            "title": "SIMF-ES-9901 Caso Especial BCI",
            "category": "Procesos",
            "ibp_id": custom_ibp["id"],
            "doc_url": "https://docs.google.com/document/d/bci9901"
        }, headers={"X-User-Role": "Coordinador"})
        self.assertEqual(esc_res.status_code, 200)
        doc = esc_res.json()["document"]
        self.assertEqual(doc["ibp_id"], custom_ibp["id"])
        self.assertEqual(doc["ibp_name"], "Banco Central de Integración")
        self.assertEqual(doc["category"], "Procesos")


if __name__ == "__main__":
    unittest.main()
