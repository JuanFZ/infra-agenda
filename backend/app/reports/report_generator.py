"""
Módulo de Generación de Reportes Ejecutivos (M9)
Agenda de Infraestructura y Base de Datos - Sycom
Soporta exportación en Excel (.xlsx) con openpyxl y vista ejecutiva imprimible en PDF/HTML.
"""

import io
import math
import datetime
from html import escape as html_escape
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func

from app.models import Item, EscalationDocument, IBP
from app.notifications.telegram_notifier import CARACAS_TZ, format_caracas_time

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


TECH_CATEGORIES = [
    "CPU",
    "RAM",
    "Disco",
    "Antivirus",
    "PostgreSQL",
    "Servicios",
    "Infraestructura",
]

# Corte oficial para estadísticas y métricas de rendimiento: 01-11-2026 00:00 (Hora Caracas UTC-4)
STATS_EPOCH_START_STR = "2026-11-01"
STATS_EPOCH_START_CARACAS = datetime.datetime(2026, 11, 1, 0, 0, 0, tzinfo=CARACAS_TZ)
STATS_EPOCH_START_UTC = STATS_EPOCH_START_CARACAS.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _infer_tech_category(text: str, fallback: str = "") -> str:
    """Infiere la categoría técnica del texto o usa el fallback."""
    t = (text or "").lower()
    if any(k in t for k in ["postgres", "postgresql", "pg_", "base de datos", "bd ", "vacuum", "query"]):
        return "PostgreSQL"
    if any(k in t for k in ["cpu", "procesador", "carga de cpu", "load average"]):
        return "CPU"
    if any(k in t for k in ["ram", "memoria", "memory", "swap", "oom"]):
        return "RAM"
    if any(k in t for k in ["disco", "disk", "filesystem", "partición", "particion", "almacenamiento", "espacio"]):
        return "Disco"
    if any(k in t for k in ["antivirus", "edr", "kaspersky", "defender", "malware", "crowdstrike"]):
        return "Antivirus"
    if any(k in t for k in ["servicio", "daemon", "systemd", "nginx", "apache", "api", "puerto", "tomcat"]):
        return "Servicios"
    if fallback and fallback in TECH_CATEGORIES:
        return fallback
    if any(k in t for k in ["servidor", "red", "switch", "firewall", "vm", "virtual", "infraestructura", "backup"]):
        return "Infraestructura"
    return fallback or "Infraestructura"


def _infer_ibp_label(text: str, ibp_rows: List[IBP]) -> Optional[str]:
    """Busca coincidencia de código o nombre de IBP en un texto."""
    if not text:
        return None
    upper_t = text.upper()
    for row in ibp_rows:
        lbl = f"{row.code} - {row.name}" if (row.code and row.code not in (row.name or "")) else (row.name or row.code or "")
        if row.code and row.code.upper() in upper_t:
            return lbl
        if row.name and len(row.name) >= 3 and row.name.upper() in upper_t:
            return lbl
    return None


def format_duration_seconds(seconds: float) -> str:
    """Convierte segundos en una cadena legible como 2h 30m o 1d 4h."""
    if seconds <= 0:
        return "0m"
    mins = int(seconds // 60)
    hours = int(mins // 60)
    days = int(hours // 24)
    if days > 0:
        return f"{days}d {hours % 24}h"
    elif hours > 0:
        return f"{hours}h {mins % 60}m"
    else:
        return f"{mins}m"


def get_report_data(
    db: Session,
    start_date: Optional[Any] = None,
    end_date: Optional[Any] = None,
    specialist_filter: Optional[Any] = None,
    ibp_filter: Optional[Any] = None,
    category_filter: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Obtiene y procesa las métricas consolidadas para reportes ejecutivos,
    incluyendo filtrado y desglose por IBP (Banco Participante) y Categoría Técnica.
    """
    query = db.query(Item)
    esc_query = db.query(EscalationDocument)

    # Sanitizar filtros de fecha y especialista interpretados en hora Caracas (UTC-4)
    clean_start = None
    if isinstance(start_date, datetime.datetime):
        clean_start = start_date
    elif isinstance(start_date, datetime.date):
        clean_start = datetime.datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0)
    elif isinstance(start_date, str) and start_date.strip():
        s = start_date.strip()
        try:
            if len(s) == 10 and s.count("-") == 2:
                y, m, d = [int(p) for p in s.split("-")]
                dt_c = datetime.datetime(y, m, d, 0, 0, 0, tzinfo=CARACAS_TZ)
                clean_start = dt_c.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            else:
                clean_start = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if clean_start.tzinfo:
                    clean_start = clean_start.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        except Exception:
            clean_start = None

    clean_end = None
    if isinstance(end_date, datetime.datetime):
        clean_end = end_date
    elif isinstance(end_date, datetime.date):
        clean_end = datetime.datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)
    elif isinstance(end_date, str) and end_date.strip():
        s = end_date.strip()
        try:
            if len(s) == 10 and s.count("-") == 2:
                y, m, d = [int(p) for p in s.split("-")]
                dt_c = datetime.datetime(y, m, d, 23, 59, 59, tzinfo=CARACAS_TZ)
                clean_end = dt_c.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            else:
                clean_end = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
                if clean_end.tzinfo:
                    clean_end = clean_end.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        except Exception:
            clean_end = None

    clean_spec = specialist_filter.strip() if isinstance(specialist_filter, str) and specialist_filter.strip() else None
    clean_ibp = ibp_filter.strip() if isinstance(ibp_filter, str) and ibp_filter.strip() else None
    clean_cat = category_filter.strip() if isinstance(category_filter, str) and category_filter.strip() else None

    # Todas las estadísticas y métricas operativas se contabilizan desde las actividades de noviembre 2026 en adelante.
    # Se utiliza el inicio real de la actividad (escalated_at -> received_at -> created_at) para no incluir
    # tickets históricos de octubre que hayan sido cerrados o sincronizados en noviembre.
    effective_start = max(clean_start, STATS_EPOCH_START_UTC) if clean_start else STATS_EPOCH_START_UTC
    item_start_expr = func.coalesce(Item.escalated_at, Item.received_at, Item.created_at)
    query = query.filter(item_start_expr >= effective_start)
    if clean_end:
        query = query.filter(item_start_expr <= clean_end)

    esc_start_expr = func.coalesce(
        EscalationDocument.escalated_at, EscalationDocument.received_at, EscalationDocument.created_at
    )
    esc_query = esc_query.filter(esc_start_expr >= effective_start)
    if clean_end:
        esc_query = esc_query.filter(esc_start_expr <= clean_end)

    if clean_spec:
        s_clean = clean_spec.lower()
        if s_clean == "sin asignar":
            query = query.filter(
                or_(
                    Item.assignee_name == None,
                    Item.assignee_name == "",
                    Item.assignee_name.ilike("%sin asignar%"),
                    Item.assignee_name.ilike("%técnico glpi%")
                )
            )
        else:
            query = query.filter(Item.assignee_name.ilike(f"%{s_clean}%"))

    raw_items: List[Item] = query.order_by(Item.created_at.desc()).all()
    raw_escalations: List[EscalationDocument] = esc_query.order_by(EscalationDocument.created_at.desc()).all()

    # Catálogo de IBPs y mapa de escalamientos por ID para enriquecer cada actividad
    ibp_catalog_rows: List[IBP] = db.query(IBP).all()
    all_escalations_map: Dict[int, EscalationDocument] = {
        e.id: e for e in db.query(EscalationDocument).all()
    }

    item_ibp_map: Dict[int, str] = {}
    item_cat_map: Dict[int, str] = {}

    for it in raw_items:
        linked_esc = all_escalations_map.get(it.escalation_id) if it.escalation_id else None
        # 1. Resolver IBP de la actividad
        resolved_ibp = None
        if linked_esc and linked_esc.ibp:
            resolved_ibp = linked_esc.ibp.strip()
        if not resolved_ibp:
            resolved_ibp = _infer_ibp_label(
                f"{it.title or ''} {it.client_name or ''} {it.description or ''}",
                ibp_catalog_rows
            )
        if not resolved_ibp:
            resolved_ibp = "General / Interno"
        item_ibp_map[it.id] = resolved_ibp

        # 2. Resolver Categoría Técnica de la actividad
        resolved_cat = None
        if linked_esc and linked_esc.category:
            resolved_cat = linked_esc.category.strip()
        if not resolved_cat:
            resolved_cat = _infer_tech_category(
                f"{it.title or ''} {it.description or ''} {it.category or ''}",
                fallback=(it.category or "Infraestructura")
            )
        item_cat_map[it.id] = resolved_cat

    # Filtrar items y escalamientos por IBP y Categoría si fueron solicitados
    items: List[Item] = []
    for it in raw_items:
        it_ibp = item_ibp_map.get(it.id, "General / Interno")
        it_cat = item_cat_map.get(it.id, "Infraestructura")
        if clean_ibp and clean_ibp.lower() not in it_ibp.lower():
            continue
        if clean_cat and clean_cat.lower() not in it_cat.lower() and clean_cat.lower() not in (it.category or "").lower():
            continue
        items.append(it)

    escalations: List[EscalationDocument] = []
    for esc in raw_escalations:
        esc_ibp = (esc.ibp or "General / Interno").strip()
        esc_cat = (esc.category or "Infraestructura").strip()
        if clean_ibp and clean_ibp.lower() not in esc_ibp.lower():
            continue
        if clean_cat and clean_cat.lower() not in esc_cat.lower():
            continue
        escalations.append(esc)

    total_items = len(items)
    cerrados = [it for it in items if it.status == "cerrado"]
    en_curso = [it for it in items if it.status == "curso"]
    pendientes = [it for it in items if it.status == "pendiente"]
    en_espera = [it for it in items if it.status == "espera"]

    # Tiempo promedio de resolución de cerrados
    total_dur_cerrados = sum(it.elapsed_seconds for it in cerrados)
    avg_resolution_seconds = (total_dur_cerrados / len(cerrados)) if cerrados else 0.0

    # Desglose por origen
    por_origen = {
        "GLPI": len([it for it in items if it.source == "GLPI"]),
        "ClickUp": len([it for it in items if it.source == "ClickUp"])
    }

    # Desglose por prioridad
    prioridades = ["Crítica", "Alta", "Media", "Baja"]
    por_prioridad = {p: len([it for it in items if it.priority == p]) for p in prioridades}

    # Desglose por Especialista
    SPECIALIST_NAMES = [
        {"name": "Adonis Lozada", "role": "Coordinador Infra & BD", "keys": ["adonis"]},
        {"name": "Juan Fernández", "role": "Especialista Infra & BD", "keys": ["juan"]},
        {"name": "Yoxaide Jiménez", "role": "Especialista Infra & BD", "keys": ["yoxaide"]},
        {"name": "Luis Acosta", "role": "Especialista Infra & BD", "keys": ["luis"]},
        {"name": "Sin asignar", "role": "Sin asignar", "keys": ["sin asignar", "técnico glpi"]}
    ]

    por_especialista = []
    for spec in SPECIALIST_NAMES:
        matched = []
        for it in items:
            assignee = (it.assignee_name or "").lower()
            if any(k in assignee for k in spec["keys"]):
                matched.append(it)
            elif not assignee and "sin asignar" in spec["keys"]:
                matched.append(it)

        s_total = len(matched)
        s_cerrados = [it for it in matched if it.status == "cerrado"]
        s_curso = [it for it in matched if it.status == "curso"]
        s_pendientes = [it for it in matched if it.status == "pendiente"]
        s_espera = [it for it in matched if it.status == "espera"]

        s_dur_total = sum(it.elapsed_seconds for it in s_cerrados)
        s_avg_dur = (s_dur_total / len(s_cerrados)) if s_cerrados else 0.0

        por_especialista.append({
            "name": spec["name"],
            "role": spec["role"],
            "total": s_total,
            "cerrados": len(s_cerrados),
            "en_curso": len(s_curso),
            "pendientes": len(s_pendientes),
            "en_espera": len(s_espera),
            "avg_seconds": round(s_avg_dur, 2),
            "avg_time_str": format_duration_seconds(s_avg_dur)
        })

    # Ordenar listado de especialistas desde el menor tiempo promedio hasta el mayor tiempo promedio
    # Perfiles sin casos cerrados y "Sin asignar" se ubican al final
    por_especialista.sort(
        key=lambda s: (
            1 if "sin asignar" in (s["name"] or "").lower() else 0,
            0 if s["cerrados"] > 0 else 1,
            s["avg_seconds"],
            -s["cerrados"],
            -s["total"],
            s["name"],
        )
    )

    # Desglose por Cliente
    client_map: Dict[str, List[Item]] = {}
    for it in items:
        c_name = it.client_name or "Interno"
        client_map.setdefault(c_name, []).append(it)

    por_cliente = []
    for c_name, c_items in sorted(client_map.items(), key=lambda x: len(x[1]), reverse=True):
        c_cerrados = [it for it in c_items if it.status == "cerrado"]
        c_activos = [it for it in c_items if it.status != "cerrado"]
        c_criticos = [it for it in c_items if it.priority == "Crítica"]
        por_cliente.append({
            "client_name": c_name,
            "client_type": c_items[0].client_type if c_items else "Interno",
            "total": len(c_items),
            "cerrados": len(c_cerrados),
            "activos": len(c_activos),
            "criticos": len(c_criticos)
        })

    # Desglose por Categoría Técnica (incluyendo las 7 categorías oficiales + adicionales activas)
    cat_items_map: Dict[str, List[Item]] = {c: [] for c in TECH_CATEGORIES}
    cat_esc_map: Dict[str, List[EscalationDocument]] = {c: [] for c in TECH_CATEGORIES}
    for it in items:
        c_lbl = item_cat_map.get(it.id, "Infraestructura")
        cat_items_map.setdefault(c_lbl, []).append(it)
    for esc in escalations:
        c_lbl = (esc.category or "Infraestructura").strip()
        cat_esc_map.setdefault(c_lbl, []).append(esc)

    all_cat_keys = list(dict.fromkeys(TECH_CATEGORIES + list(cat_items_map.keys()) + list(cat_esc_map.keys())))
    por_categoria = []
    for c_lbl in all_cat_keys:
        if clean_cat and clean_cat.lower() not in c_lbl.lower():
            continue
        c_its = cat_items_map.get(c_lbl, [])
        c_escs = cat_esc_map.get(c_lbl, [])
        linked_esc_ids = {it.escalation_id for it in c_its if it.escalation_id}
        unlinked_escs = [e for e in c_escs if e.id not in linked_esc_ids]
        total_vol = len(c_its) + len(unlinked_escs)
        c_cerr = [it for it in c_its if it.status == "cerrado"]
        c_curso = [it for it in c_its if it.status == "curso"]
        c_espera = [it for it in c_its if it.status == "espera"]
        c_pend = [it for it in c_its if it.status == "pendiente"]
        c_act = [it for it in c_its if it.status != "cerrado"]
        por_categoria.append({
            "category": c_lbl,
            "total": total_vol,
            "total_items": len(c_its),
            "total_escalamientos": len(c_escs),
            "cerrados": len(c_cerr),
            "en_curso": len(c_curso),
            "en_espera": len(c_espera),
            "pendientes": len(c_pend),
            "activos": len(c_act) + len(unlinked_escs),
        })
    por_categoria.sort(key=lambda x: (x["total"], x["total_escalamientos"]), reverse=True)

    # Desglose por IBP (Banco Participante)
    ibp_items_map: Dict[str, List[Item]] = {}
    ibp_esc_map: Dict[str, List[EscalationDocument]] = {}
    for it in items:
        i_lbl = item_ibp_map.get(it.id, "General / Interno")
        ibp_items_map.setdefault(i_lbl, []).append(it)
    for esc in escalations:
        i_lbl = (esc.ibp or "General / Interno").strip()
        ibp_esc_map.setdefault(i_lbl, []).append(esc)

    all_ibp_keys = list(dict.fromkeys(list(ibp_esc_map.keys()) + list(ibp_items_map.keys())))
    if clean_ibp and not all_ibp_keys:
        all_ibp_keys = [clean_ibp]

    por_ibp = []
    for i_lbl in all_ibp_keys:
        i_its = ibp_items_map.get(i_lbl, [])
        i_escs = ibp_esc_map.get(i_lbl, [])
        linked_esc_ids = {it.escalation_id for it in i_its if it.escalation_id}
        unlinked_escs = [e for e in i_escs if e.id not in linked_esc_ids]
        total_vol = len(i_its) + len(unlinked_escs)
        i_cerr = [it for it in i_its if it.status == "cerrado"]
        i_curso = [it for it in i_its if it.status == "curso"]
        i_espera = [it for it in i_its if it.status == "espera"]
        i_pend = [it for it in i_its if it.status == "pendiente"]
        i_act = [it for it in i_its if it.status != "cerrado"]
        por_ibp.append({
            "ibp": i_lbl,
            "total": total_vol,
            "total_items": len(i_its),
            "total_escalamientos": len(i_escs),
            "cerrados": len(i_cerr),
            "en_curso": len(i_curso),
            "en_espera": len(i_espera),
            "pendientes": len(i_pend),
            "activos": len(i_act) + len(unlinked_escs),
        })
    por_ibp.sort(key=lambda x: (0 if x["ibp"] != "General / Interno" else 1, -x["total"], -x["total_escalamientos"]))

    # Detalle de Escalamientos
    escalations_detail = []
    for esc in escalations:
        escalations_detail.append({
            "id": esc.id,
            "title": esc.title or "",
            "ibp": esc.ibp or "General / Interno",
            "category": esc.category or "Infraestructura",
            "received_at_str": format_caracas_time(esc.received_at) if esc.received_at else (format_caracas_time(esc.created_at) if esc.created_at else "—"),
            "escalated_at_str": format_caracas_time(esc.escalated_at) if esc.escalated_at else (format_caracas_time(esc.created_at) if esc.created_at else "—"),
            "recommendations": esc.recommendations or "",
            "notes": esc.notes or "",
            "google_doc_url": esc.doc_url or "",
            "created_by": esc.uploaded_by or "Coordinación",
        })

    # Detalle de Items
    items_detail = []
    for it in items:
        dur_str = it.duration_human

        items_detail.append({
            "id": it.id,
            "source": it.source,
            "external_id": it.external_id,
            "url": it.url or "",
            "title": it.title or "",
            "client_name": it.client_name or "Interno",
            "client_type": it.client_type or "Interno",
            "ibp": item_ibp_map.get(it.id, "General / Interno"),
            "category": item_cat_map.get(it.id, "Infraestructura"),
            "escalation_id": it.escalation_id,
            "assignee_name": it.assignee_name or "Sin asignar",
            "priority": it.priority or "Media",
            "status": it.status or "pendiente",
            "status_origin": it.status_origin or it.status or "",
            "duration_str": dur_str,
            "attention_time_str": it.time_attention_label,
            "received_at_str": format_caracas_time(it.received_at or it.created_at) if (it.received_at or it.created_at) else "—",
            "escalated_at_str": format_caracas_time(it.escalated_at or it.received_at or it.created_at) if (it.escalated_at or it.received_at or it.created_at) else "—",
            "created_at_str": format_caracas_time(it.created_at) if it.created_at else "—",
            "assigned_at_str": format_caracas_time(it.assigned_at) if it.assigned_at else "—",
            "closed_at_str": format_caracas_time(it.closed_at) if it.closed_at else "—",
        })

    # Determinación de etiquetas de fechas (estadísticas válidas desde 01-11-2026)
    now_caracas = datetime.datetime.now(CARACAS_TZ)
    effective_start_str = format_caracas_time(effective_start, '%d-%m-%Y')
    if not clean_start and not clean_end:
        period_str = f"Desde {effective_start_str} ({now_caracas.strftime('%B %Y').capitalize()})"
    elif clean_end:
        period_str = f"{effective_start_str} al {format_caracas_time(clean_end, '%d-%m-%Y')}"
    else:
        period_str = f"Desde {effective_start_str}"

    return {
        "generated_at": now_caracas.strftime("%d-%m-%Y %I:%M %p"),
        "period_label": period_str,
        "filters": {
            "specialist": clean_spec or "Todo el equipo",
            "ibp": clean_ibp or "Todas las IBPs",
            "category": clean_cat or "Todas las Categorías",
        },
        "kpis": {
            "total_items": total_items,
            "total_escalamientos": len(escalations),
            "total_cerrados": len(cerrados),
            "total_en_curso": len(en_curso),
            "total_pendientes": len(pendientes),
            "total_en_espera": len(en_espera),
            "avg_resolution_str": format_duration_seconds(avg_resolution_seconds)
        },
        "por_origen": por_origen,
        "por_prioridad": por_prioridad,
        "por_especialista": por_especialista,
        "por_cliente": por_cliente,
        "por_ibp": por_ibp,
        "por_categoria": por_categoria,
        "escalations": escalations_detail,
        "items": items_detail
    }


def generate_excel_report(data: Dict[str, Any]) -> io.BytesIO:
    """
    Construye un libro de Excel (.xlsx) corporativo con 4 hojas formateadas profesionalmente.
    """
    wb = openpyxl.Workbook()
    
    # Paleta de Colores Corporativos Sycom
    NAVY_DARK = "1B365D"     # Azul corporativo profundo
    NAVY_LIGHT = "2C4D75"    # Azul medio para subencabezados
    ACCENT_BLUE = "2563EB"   # Azul acento
    BG_LIGHT_GRAY = "F8FAFC" # Fondo suave
    BORDER_GRAY = "CBD5E1"   # Borde suave
    
    font_title = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    font_subtitle = Font(name="Calibri", size=10, italic=True, color="E2E8F0")
    font_section = Font(name="Calibri", size=12, bold=True, color="1B365D")
    font_header = Font(name="Calibri", size=10.5, bold=True, color="FFFFFF")
    font_body = Font(name="Calibri", size=10, color="0F172A")
    font_body_bold = Font(name="Calibri", size=10, bold=True, color="0F172A")
    font_kpi_num = Font(name="Calibri", size=18, bold=True, color="1B365D")
    font_kpi_lbl = Font(name="Calibri", size=9, bold=True, color="64748B")

    fill_header = PatternFill(start_color=NAVY_DARK, end_color=NAVY_DARK, fill_type="solid")
    fill_subheader = PatternFill(start_color=NAVY_LIGHT, end_color=NAVY_LIGHT, fill_type="solid")
    fill_zebra = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    fill_kpi = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

    thin_border_side = Side(border_style="thin", color=BORDER_GRAY)
    border_cell = Border(left=thin_border_side, right=thin_border_side, top=thin_border_side, bottom=thin_border_side)
    border_kpi = Border(
        left=Side(border_style="medium", color=ACCENT_BLUE),
        right=thin_border_side, top=thin_border_side, bottom=thin_border_side
    )

    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")

    # ==========================================
    # HOJA 1: RESUMEN EJECUTIVO
    # ==========================================
    ws_resumen = wb.active
    ws_resumen.title = "Resumen Ejecutivo"
    ws_resumen.views.sheetView[0].showGridLines = True

    # Banner de Título
    ws_resumen.merge_cells("A1:G1")
    ws_resumen["A1"] = "SOLUCIONES SYCOM — AGENDA DE INFRAESTRUCTURA & BD"
    ws_resumen["A1"].font = font_title
    ws_resumen["A1"].fill = fill_header
    ws_resumen["A1"].alignment = align_center

    ws_resumen.merge_cells("A2:G2")
    ws_resumen["A2"] = f"INFORME EJECUTIVO DE GESTIÓN OPERATIVA · {data['period_label'].upper()}"
    ws_resumen["A2"].font = font_subtitle
    ws_resumen["A2"].fill = fill_subheader
    ws_resumen["A2"].alignment = align_center

    ws_resumen["A4"] = f"Fecha de Emisión: {data['generated_at']} (Hora Legal de Caracas, UTC-4)"
    ws_resumen["A4"].font = Font(name="Calibri", size=9.5, italic=True, color="64748B")

    # Tarjetas KPI Principales (Fila 6 y 7)
    kpis = data["kpis"]
    kpi_cards = [
        ("A", "A", "TOTAL ASUNTOS", str(kpis["total_items"]), "1B365D"),
        ("B", "B", "CERRADOS", str(kpis["total_cerrados"]), "15803D"),
        ("C", "C", "EN ATENCIÓN", str(kpis["total_en_curso"]), "0891B2"),
        ("D", "D", "PENDIENTES", str(kpis["total_pendientes"]), "2563EB"),
        ("E", "E", "EN ESPERA", str(kpis["total_en_espera"]), "D97706"),
        ("F", "G", "ESCALAMIENTOS", str(kpis.get("total_escalamientos", 0)), "2563EB")
    ]

    for col_start, col_end, lbl, val, color_hex in kpi_cards:
        if col_start != col_end:
            ws_resumen.merge_cells(f"{col_start}6:{col_end}6")
            ws_resumen.merge_cells(f"{col_start}7:{col_end}7")
        c_lbl = ws_resumen[f"{col_start}6"]
        c_lbl.value = lbl
        c_lbl.font = font_kpi_lbl
        c_lbl.alignment = align_center
        c_lbl.fill = fill_kpi

        c_val = ws_resumen[f"{col_start}7"]
        c_val.value = val
        c_val.font = Font(name="Calibri", size=18, bold=True, color=color_hex)
        c_val.alignment = align_center
        c_val.fill = fill_kpi

        for row in (6, 7):
            for c_letter in ([col_start] if col_start == col_end else [col_start, col_end]):
                ws_resumen[f"{c_letter}{row}"].border = border_cell

    # Indicador de Tiempo Promedio
    ws_resumen["A9"] = f"Tiempo Promedio de Resolución: {kpis['avg_resolution_str']}"
    ws_resumen["A9"].font = Font(name="Calibri", size=11, bold=True, color="1B365D")

    # Tabla 1. Distribución por Plataforma (Origen)
    ws_resumen["A11"] = "1. DISTRIBUCIÓN POR PLATAFORMA DE ORIGEN"
    ws_resumen["A11"].font = font_section

    headers_orig = ["Plataforma / Sistema", "Total Tickets / Tareas", "% del Total", "Estado Operativo"]
    for col_idx, h in enumerate(headers_orig, start=1):
        cell = ws_resumen.cell(row=12, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    row_cur = 13
    for orig, cnt in data["por_origen"].items():
        pct = (cnt / kpis["total_items"] * 100) if kpis["total_items"] > 0 else 0
        ws_resumen.cell(row=row_cur, column=1, value=orig).alignment = align_left
        ws_resumen.cell(row=row_cur, column=2, value=cnt).alignment = align_center
        ws_resumen.cell(row=row_cur, column=3, value=f"{pct:.1f}%").alignment = align_center
        ws_resumen.cell(row=row_cur, column=4, value="Sincronizado OK").alignment = align_center
        for c in range(1, 5):
            ws_resumen.cell(row=row_cur, column=c).border = border_cell
            ws_resumen.cell(row=row_cur, column=c).font = font_body
        row_cur += 1

    # Tabla 2. Distribución por Nivel de Prioridad
    row_cur += 1
    ws_resumen.cell(row=row_cur, column=1, value="2. DISTRIBUCIÓN POR NIVEL DE PRIORIDAD").font = font_section
    row_cur += 1

    headers_prio = ["Nivel de Prioridad", "Cantidad de Casos", "% del Total", "Nivel de Atención"]
    prio_levels = {
        "Crítica": "Inmediato (Crítica)",
        "Alta": "Prioritario (Alta)",
        "Media": "Estándar (Media)",
        "Baja": "Planificado (Baja)"
    }
    for col_idx, h in enumerate(headers_prio, start=1):
        cell = ws_resumen.cell(row=row_cur, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    row_cur += 1
    for prio, cnt in data["por_prioridad"].items():
        pct = (cnt / kpis["total_items"] * 100) if kpis["total_items"] > 0 else 0
        ws_resumen.cell(row=row_cur, column=1, value=prio).alignment = align_left
        ws_resumen.cell(row=row_cur, column=2, value=cnt).alignment = align_center
        ws_resumen.cell(row=row_cur, column=3, value=f"{pct:.1f}%").alignment = align_center
        ws_resumen.cell(row=row_cur, column=4, value=prio_levels.get(prio, "—")).alignment = align_center
        for c in range(1, 5):
            ws_resumen.cell(row=row_cur, column=c).border = border_cell
            ws_resumen.cell(row=row_cur, column=c).font = font_body
        row_cur += 1

    # Ajuste de ancho de columnas
    for col in ws_resumen.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws_resumen.column_dimensions[col_letter].width = max(max_len + 3, 14)

    # ==========================================
    # HOJA 2: POR ESPECIALISTA
    # ==========================================
    ws_spec = wb.create_sheet(title="Por Especialista")
    ws_spec.views.sheetView[0].showGridLines = True

    ws_spec.merge_cells("A1:H1")
    ws_spec["A1"] = "RENDIMIENTO Y CARGA DE TRABAJO POR ESPECIALISTA"
    ws_spec["A1"].font = font_title
    ws_spec["A1"].fill = fill_header
    ws_spec["A1"].alignment = align_center

    ws_spec.merge_cells("A2:H2")
    ws_spec["A2"] = f"Auditoría Individual de Productividad y Casos Atendidos · {data['period_label']}"
    ws_spec["A2"].font = font_subtitle
    ws_spec["A2"].fill = fill_subheader
    ws_spec["A2"].alignment = align_center

    headers_spec = [
        "Especialista", "Rol Operativo", "Total Asignadas", 
        "Cerradas", "En Atención", "En Espera", "Pendientes", "Tiempo Promedio"
    ]
    for col_idx, h in enumerate(headers_spec, start=1):
        cell = ws_spec.cell(row=4, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    for r_idx, s in enumerate(data["por_especialista"], start=5):
        row_fill = fill_zebra if r_idx % 2 == 0 else PatternFill(fill_type=None)
        ws_spec.cell(row=r_idx, column=1, value=s["name"]).alignment = align_left
        ws_spec.cell(row=r_idx, column=2, value=s["role"]).alignment = align_left
        ws_spec.cell(row=r_idx, column=3, value=s["total"]).alignment = align_center
        ws_spec.cell(row=r_idx, column=4, value=s["cerrados"]).alignment = align_center
        ws_spec.cell(row=r_idx, column=5, value=s["en_curso"]).alignment = align_center
        ws_spec.cell(row=r_idx, column=6, value=s.get("en_espera", 0)).alignment = align_center
        ws_spec.cell(row=r_idx, column=7, value=s["pendientes"]).alignment = align_center
        ws_spec.cell(row=r_idx, column=8, value=s["avg_time_str"]).alignment = align_center

        for c in range(1, 9):
            cell = ws_spec.cell(row=r_idx, column=c)
            cell.border = border_cell
            cell.font = font_body
            if row_fill.fill_type:
                cell.fill = row_fill

    for col in ws_spec.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws_spec.column_dimensions[col_letter].width = max(max_len + 3, 14)

    # ==========================================
    # HOJA 3: POR CLIENTE
    # ==========================================
    ws_client = wb.create_sheet(title="Por Cliente")
    ws_client.views.sheetView[0].showGridLines = True

    ws_client.merge_cells("A1:F1")
    ws_client["A1"] = "VOLUMEN DE SERVICIOS POR CLIENTE / ENTIDAD"
    ws_client["A1"].font = font_title
    ws_client["A1"].fill = fill_header
    ws_client["A1"].alignment = align_center

    ws_client.merge_cells("A2:F2")
    ws_client["A2"] = f"Consolidación por Cliente / Espacio Operativo · {data['period_label']}"
    ws_client["A2"].font = font_subtitle
    ws_client["A2"].fill = fill_subheader
    ws_client["A2"].alignment = align_center

    headers_client = [
        "Cliente / Espacio", "Tipo de Entidad", "Total Requerimientos",
        "Resueltos", "En Atención Activa", "Casos Críticos"
    ]
    for col_idx, h in enumerate(headers_client, start=1):
        cell = ws_client.cell(row=4, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    for r_idx, c in enumerate(data["por_cliente"], start=5):
        row_fill = fill_zebra if r_idx % 2 == 0 else PatternFill(fill_type=None)
        ws_client.cell(row=r_idx, column=1, value=c["client_name"]).alignment = align_left
        ws_client.cell(row=r_idx, column=2, value=c["client_type"]).alignment = align_center
        ws_client.cell(row=r_idx, column=3, value=c["total"]).alignment = align_center
        ws_client.cell(row=r_idx, column=4, value=c["cerrados"]).alignment = align_center
        ws_client.cell(row=r_idx, column=5, value=c["activos"]).alignment = align_center
        ws_client.cell(row=r_idx, column=6, value=c["criticos"]).alignment = align_center

        for col_i in range(1, 7):
            cell = ws_client.cell(row=r_idx, column=col_i)
            cell.border = border_cell
            cell.font = font_body
            if row_fill.fill_type:
                cell.fill = row_fill

    for col in ws_client.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws_client.column_dimensions[col_letter].width = max(max_len + 3, 15)

    # ==========================================
    # HOJA 4: DETALLE DE ACTIVIDADES
    # ==========================================
    ws_items = wb.create_sheet(title="Detalle Actividades")
    ws_items.views.sheetView[0].showGridLines = True

    ws_items.merge_cells("A1:N1")
    ws_items["A1"] = "INVENTARIO DETALLADO DE TICKETS Y ACTIVIDADES OPERATIVAS"
    ws_items["A1"].font = font_title
    ws_items["A1"].fill = fill_header
    ws_items["A1"].alignment = align_center

    headers_detail = [
        "# ID", "Origen", "ID Externo", "Asunto / Título del Requerimiento",
        "Cliente / Entidad", "Responsable(s)", "Prioridad", "Estado Actual",
        "Tiempo de Atención", "Fecha Recibido Sycom", "Fecha Escalado Infra",
        "Fecha Asignación", "Fecha Cierre", "Enlace Nativo"
    ]
    for col_idx, h in enumerate(headers_detail, start=1):
        cell = ws_items.cell(row=3, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    for r_idx, it in enumerate(data["items"], start=4):
        row_fill = fill_zebra if r_idx % 2 == 0 else PatternFill(fill_type=None)
        ws_items.cell(row=r_idx, column=1, value=it["id"]).alignment = align_center
        ws_items.cell(row=r_idx, column=2, value=it["source"]).alignment = align_center
        ws_items.cell(row=r_idx, column=3, value=it["external_id"]).alignment = align_center
        ws_items.cell(row=r_idx, column=4, value=it["title"]).alignment = align_left
        ws_items.cell(row=r_idx, column=5, value=it["client_name"]).alignment = align_left
        ws_items.cell(row=r_idx, column=6, value=it["assignee_name"]).alignment = align_left
        
        # Prioridad con color suave
        c_prio = ws_items.cell(row=r_idx, column=7, value=it["priority"])
        c_prio.alignment = align_center

        ws_items.cell(row=r_idx, column=8, value=it["status_origin"] or it["status"]).alignment = align_center
        ws_items.cell(row=r_idx, column=9, value=it.get("attention_time_str") or it["duration_str"]).alignment = align_center
        ws_items.cell(row=r_idx, column=10, value=it["received_at_str"]).alignment = align_center
        ws_items.cell(row=r_idx, column=11, value=it["escalated_at_str"]).alignment = align_center
        ws_items.cell(row=r_idx, column=12, value=it["assigned_at_str"]).alignment = align_center
        ws_items.cell(row=r_idx, column=13, value=it["closed_at_str"]).alignment = align_center
        
        c_url = ws_items.cell(row=r_idx, column=14, value=it["url"])
        c_url.alignment = align_left
        if it["url"]:
            c_url.hyperlink = it["url"]
            c_url.font = Font(name="Calibri", size=9, color="2563EB", underline="single")

        for col_i in range(1, 15):
            cell = ws_items.cell(row=r_idx, column=col_i)
            cell.border = border_cell
            if col_i != 14:
                cell.font = font_body
            if row_fill.fill_type:
                cell.fill = row_fill

    # Anchos para Hoja Detalle
    widths = {1: 8, 2: 11, 3: 13, 4: 42, 5: 24, 6: 28, 7: 12, 8: 16, 9: 18, 10: 19, 11: 19, 12: 19, 13: 19, 14: 30}
    for col_idx, w in widths.items():
        col_letter = get_column_letter(col_idx)
        ws_items.column_dimensions[col_letter].width = w

    # ==========================================
    # HOJA 5: POR IBP Y CATEGORÍA
    # ==========================================
    ws_ibp = wb.create_sheet(title="Por IBP y Categoría")
    ws_ibp.views.sheetView[0].showGridLines = True

    ws_ibp.merge_cells("A1:G1")
    ws_ibp["A1"] = "DESGLOSE EJECUTIVO POR IBP (BANCO PARTICIPANTE) Y CATEGORÍA TÉCNICA"
    ws_ibp["A1"].font = font_title
    ws_ibp["A1"].fill = fill_header
    ws_ibp["A1"].alignment = align_center

    ws_ibp["A3"] = "1. MÉTRICAS POR IBP (BANCO PARTICIPANTE)"
    ws_ibp["A3"].font = font_section

    headers_ibp = ["IBP / Entidad Bancaria", "Volumen Total", "Actividades", "Escalamientos", "Cerrados", "En Curso", "En Espera"]
    for col_idx, h in enumerate(headers_ibp, start=1):
        cell = ws_ibp.cell(row=4, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    row_cursor = 5
    for ibp_row in data.get("por_ibp", []):
        ws_ibp.cell(row=row_cursor, column=1, value=ibp_row["ibp"]).alignment = align_left
        ws_ibp.cell(row=row_cursor, column=2, value=ibp_row["total"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=3, value=ibp_row["total_items"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=4, value=ibp_row["total_escalamientos"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=5, value=ibp_row["cerrados"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=6, value=ibp_row.get("en_curso", 0)).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=7, value=ibp_row.get("en_espera", 0)).alignment = align_center
        for c_i in range(1, 8):
            c_cell = ws_ibp.cell(row=row_cursor, column=c_i)
            c_cell.font = font_body
            c_cell.border = border_cell
        row_cursor += 1

    row_cursor += 2
    ws_ibp.cell(row=row_cursor, column=1, value="2. MÉTRICAS POR CATEGORÍA TÉCNICA").font = font_section
    row_cursor += 1

    headers_cat = ["Categoría Técnica", "Volumen Total", "Actividades", "Escalamientos", "Cerrados", "En Curso", "En Espera"]
    for col_idx, h in enumerate(headers_cat, start=1):
        cell = ws_ibp.cell(row=row_cursor, column=col_idx, value=h)
        cell.font = font_header
        cell.fill = fill_header
        cell.alignment = align_center
        cell.border = border_cell

    row_cursor += 1
    for cat_row in data.get("por_categoria", []):
        ws_ibp.cell(row=row_cursor, column=1, value=cat_row["category"]).alignment = align_left
        ws_ibp.cell(row=row_cursor, column=2, value=cat_row["total"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=3, value=cat_row["total_items"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=4, value=cat_row["total_escalamientos"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=5, value=cat_row["cerrados"]).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=6, value=cat_row.get("en_curso", 0)).alignment = align_center
        ws_ibp.cell(row=row_cursor, column=7, value=cat_row.get("en_espera", 0)).alignment = align_center
        for c_i in range(1, 8):
            c_cell = ws_ibp.cell(row=row_cursor, column=c_i)
            c_cell.font = font_body
            c_cell.border = border_cell
        row_cursor += 1

    for col_idx, w in {1: 30, 2: 15, 3: 15, 4: 16, 5: 14, 6: 16, 7: 16}.items():
        ws_ibp.column_dimensions[get_column_letter(col_idx)].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def _build_svg_donut_chart(kpis: Dict[str, Any]) -> str:
    """Construye un gráfico vectorial SVG tipo Donut para el reporte ejecutivo PDF."""
    total = kpis.get("total_items", 0)
    cerrados = kpis.get("total_cerrados", 0)
    en_curso = kpis.get("total_en_curso", 0)
    pendientes = kpis.get("total_pendientes", 0)
    en_espera = kpis.get("total_en_espera", 0)

    r = 46
    c_len = 2 * math.pi * r
    slices = [
        {"label": "Cerrados", "count": cerrados, "color": "#10b981"},
        {"label": "En Curso", "count": en_curso, "color": "#0ea5e9"},
        {"label": "Pendientes", "count": pendientes, "color": "#2563eb"},
        {"label": "En Espera", "count": en_espera, "color": "#f59e0b"},
    ]

    circles_svg = ""
    offset = 0.0
    if total > 0:
        for s in slices:
            if s["count"] <= 0:
                continue
            dash = (s["count"] / total) * c_len
            circles_svg += (
                f'<circle r="{r}" cx="65" cy="65" fill="transparent" stroke="{s["color"]}" '
                f'stroke-width="18" stroke-dasharray="{dash:.2f} {c_len:.2f}" '
                f'stroke-dashoffset="-{offset:.2f}" transform="rotate(-90 65 65)" />'
            )
            offset += dash
    else:
        circles_svg = f'<circle r="{r}" cx="65" cy="65" fill="transparent" stroke="#cbd5e1" stroke-width="18" />'

    legend_rows = ""
    for s in slices:
        pct = ((s["count"] / total) * 100.0) if total > 0 else 0.0
        legend_rows += f"""
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;padding:3px 6px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:5px;margin-bottom:4px">
            <span style="display:flex;align-items:center;gap:6px;font-weight:600;color:#1e293b">
                <span style="width:9px;height:9px;border-radius:50%;background:{s['color']};display:inline-block"></span>
                {s['label']}
            </span>
            <span style="font-weight:700;color:#0f172a">{s['count']} <small style="color:#64748b">({pct:.0f}%)</small></span>
        </div>
        """

    return f"""
    <div style="display:flex;align-items:center;gap:14px">
        <div style="position:relative;width:130px;height:130px;flex-shrink:0">
            <svg viewBox="0 0 130 130" width="130" height="130">
                <circle r="{r}" cx="65" cy="65" fill="transparent" stroke="#f1f5f9" stroke-width="18" />
                {circles_svg}
            </svg>
            <div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
                <span style="font-size:18px;font-weight:800;color:#0f172a;line-height:1">{total}</span>
                <span style="font-size:8.5px;font-weight:700;color:#64748b;text-transform:uppercase;margin-top:2px">Actividades</span>
            </div>
        </div>
        <div style="flex:1">
            {legend_rows}
            <div style="font-size:9.5px;color:#475569;margin-top:4px;text-align:right">
                Total Operativo: <strong>{total}</strong>
            </div>
        </div>
    </div>
    """


# ---------------------------------------------------------------------------
# Helpers del PDF: barras apiladas por estado y matriz IBP × Categoría
# ---------------------------------------------------------------------------
GENERAL_IBP_LABEL = "General / Interno"

PDF_CAT_COLORS = {
    "CPU": "#ef4444",
    "RAM": "#f97316",
    "Disco": "#eab308",
    "Antivirus": "#10b981",
    "PostgreSQL": "#3b82f6",
    "Servicios": "#8b5cf6",
    "Infraestructura": "#06b6d4",
    "Proyectos y Tareas": "#ec4899",
}

# (clave, etiqueta, plural, singular, color) — segmentos mutuamente excluyentes
PDF_SEGMENTS = [
    ("cerrados", "Cerradas", "cerradas", "cerrada", "#10b981"),
    ("en_curso", "En Curso", "en curso", "en curso", "#0ea5e9"),
    ("en_espera", "En Espera", "en espera", "en espera", "#f59e0b"),
    ("pendientes", "Pendientes", "pendientes", "pendiente", "#2563eb"),
    ("esc_only", "Escalamiento sin actividad", "esc. sin act.", "esc. sin act.", "#8b5cf6"),
]


def _pdf_breakdown(items: List[Dict[str, Any]], field: str, default: str) -> Dict[str, Dict[str, int]]:
    """Agrupa los items del período por IBP o categoría en segmentos de estado operativos."""
    out: Dict[str, Dict[str, int]] = {}
    for it in items:
        key = it.get(field) or default
        b = out.setdefault(key, {"cerrados": 0, "en_curso": 0, "en_espera": 0, "pendientes": 0})
        st = it.get("status")
        if st == "cerrado":
            b["cerrados"] += 1
        elif st == "espera":
            b["en_espera"] += 1
        elif st == "curso":
            b["en_curso"] += 1
        else:
            b["pendientes"] += 1
    return out


def _pdf_row_segments(row: Dict[str, Any], b: Optional[Dict[str, int]]) -> Dict[str, int]:
    total_items = row.get("total_items", 0) or 0
    if not b or sum(b.values()) != total_items:
        cerrados = row.get("cerrados", 0) or 0
        en_curso = row.get("en_curso", 0) or 0
        en_espera = row.get("en_espera", 0) or 0
        pendientes = max(total_items - cerrados - en_curso - en_espera, 0)
        b = {"cerrados": cerrados, "en_curso": en_curso, "en_espera": en_espera, "pendientes": pendientes}
    return {**b, "esc_only": max((row.get("total", 0) or 0) - total_items, 0)}


def _pdf_segments_legend() -> str:
    chips = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:10px">'
        f'<span style="width:8px;height:8px;border-radius:2px;background:{color};display:inline-block"></span>{label}</span>'
        for _, label, _, _, color in PDF_SEGMENTS
    )
    return f'<div style="font-size:9px;color:#475569;margin-bottom:6px;display:flex;flex-wrap:wrap">{chips}</div>'


def _pdf_bar_row(label: str, label_color: str, dot_color: Optional[str], segs: Dict[str, int],
                 total: int, max_total: int, sum_total: int) -> str:
    width = max((total / max_total) * 100.0, 4.0) if total > 0 and max_total > 0 else 0.0
    share = (total / sum_total) * 100.0 if sum_total > 0 else 0.0
    parts = "".join(
        f'<div style="width:{(segs[k] / total) * 100.0:.2f}%;background:{color};height:100%"></div>'
        for k, _, _, _, color in PDF_SEGMENTS if total > 0 and segs.get(k, 0) > 0
    )
    detail = " · ".join(
        f'<span style="color:{color}">{segs[k]} {one if segs[k] == 1 else plural}</span>'
        for k, _, plural, one, color in PDF_SEGMENTS if segs.get(k, 0) > 0
    )
    dot = (f'<span style="width:8px;height:8px;border-radius:2px;background:{dot_color};display:inline-block"></span>'
           if dot_color else "")
    return f"""
        <div style="margin-bottom:7px">
            <div style="display:flex;justify-content:space-between;align-items:center;font-size:10.5px;margin-bottom:2px">
                <span style="font-weight:700;color:{label_color};display:flex;align-items:center;gap:5px">{dot}{html_escape(label)}</span>
                <span style="font-size:10px;color:#334155"><strong>{total}</strong> ({share:.0f}%)</span>
            </div>
            <div style="background:#e2e8f0;height:7px;border-radius:4px;overflow:hidden">
                <div style="width:{width:.1f}%;height:100%;display:flex">{parts}</div>
            </div>
            <div style="font-size:9px;margin-top:2px">{detail}</div>
        </div>
    """


def _pdf_ibp_category_matrix(data: Dict[str, Any]) -> str:
    """Tabla de calor IBP × Categoría para el informe imprimible."""
    items = data.get("items", []) or []
    escs = data.get("escalations", []) or []
    linked_ids = {it.get("escalation_id") for it in items if it.get("escalation_id")}

    cells: Dict[tuple, int] = {}
    row_tot: Dict[str, int] = {}
    col_tot: Dict[str, int] = {}

    def _add(ibp: str, cat: str) -> None:
        cells[(ibp, cat)] = cells.get((ibp, cat), 0) + 1
        row_tot[ibp] = row_tot.get(ibp, 0) + 1
        col_tot[cat] = col_tot.get(cat, 0) + 1

    for it in items:
        _add(it.get("ibp") or GENERAL_IBP_LABEL, it.get("category") or "Infraestructura")
    for e in escs:
        if e.get("id") in linked_ids:
            continue
        _add((e.get("ibp") or GENERAL_IBP_LABEL).strip(), (e.get("category") or "Infraestructura").strip())

    if not row_tot:
        return '<div style="padding:12px;text-align:center;color:#64748b;font-size:11px">Sin casos para cruzar en este período.</div>'

    rows = sorted(row_tot.keys(), key=lambda r: (r == GENERAL_IBP_LABEL, -row_tot[r], r))
    cols = sorted(col_tot.keys(), key=lambda c: (-col_tot[c], c))
    max_cell = max(cells.values()) if cells else 1
    grand = sum(row_tot.values())

    head = "".join(
        f'<th style="text-align:center;white-space:nowrap">'
        f'<span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{PDF_CAT_COLORS.get(c, "#2563eb")};margin-right:4px"></span>'
        f'{html_escape(c)}</th>'
        for c in cols
    )
    body = ""
    for r in rows:
        tds = ""
        for c in cols:
            cnt = cells.get((r, c), 0)
            if not cnt:
                tds += '<td style="text-align:center;color:#cbd5e1;background:#ffffff">·</td>'
                continue
            alpha = 0.12 + 0.68 * (cnt / max_cell)
            strong = alpha > 0.45
            tds += (f'<td style="text-align:center;font-weight:800;background:rgba(37,99,235,{alpha:.2f});'
                    f'color:{"#ffffff" if strong else "#0f172a"}">{cnt}</td>')
        name_color = "#64748b" if r == GENERAL_IBP_LABEL else "#1b365d"
        body += (f'<tr><td style="font-weight:700;white-space:nowrap;color:{name_color};background:#ffffff">{html_escape(r)}</td>'
                 f'{tds}<td style="text-align:center;font-weight:800;background:#f1f5f9">{row_tot[r]}</td></tr>')
    foot = "".join(f'<td style="text-align:center;font-weight:800;background:#f1f5f9">{col_tot[c]}</td>' for c in cols)
    body += (f'<tr><td style="font-weight:800;text-transform:uppercase;font-size:9.5px;background:#f1f5f9">Total</td>{foot}'
             f'<td style="text-align:center;font-weight:900;color:#1b365d;background:#e2e8f0">{grand}</td></tr>')

    return f"""
        <table style="margin-bottom:6px">
            <thead><tr><th>IBP \\ Categoría</th>{head}<th style="text-align:center">Total</th></tr></thead>
            <tbody>{body}</tbody>
        </table>
        <div style="font-size:9px;color:#64748b;display:flex;gap:16px;align-items:center">
            <span>Intensidad = volumen de casos
                <span style="display:inline-block;width:70px;height:7px;border-radius:4px;vertical-align:middle;margin-left:4px;background:linear-gradient(90deg, rgba(37,99,235,0.12), rgba(37,99,235,0.80))"></span>
            </span>
        </div>
    """


def generate_executive_html(data: Dict[str, Any]) -> str:
    """
    Genera un informe visual ejecutivo imprimible en PDF con KPIs, gráficos vectoriales (SVG/Barras)
    y desglose por IBP (Banco Participante), Categoría Técnica, Especialista y Escalamientos.
    """
    kpis = data["kpis"]
    filters = data.get("filters", {})
    ibp_filter_lbl = filters.get("ibp", "Todas las IBPs")
    cat_filter_lbl = filters.get("category", "Todas las Categorías")
    spec_filter_lbl = filters.get("specialist", "Todo el equipo")

    # Subtítulo dinámico si el reporte está filtrado por IBP o por Categoría
    scope_title_parts = []
    if ibp_filter_lbl and ibp_filter_lbl != "Todas las IBPs":
        scope_title_parts.append(f"IBP: {ibp_filter_lbl}")
    if cat_filter_lbl and cat_filter_lbl != "Todas las Categorías":
        scope_title_parts.append(f"CATEGORÍA: {cat_filter_lbl}")
    if spec_filter_lbl and spec_filter_lbl != "Todo el equipo":
        scope_title_parts.append(f"ESPECIALISTA: {spec_filter_lbl}")
    dynamic_report_heading = " · ".join(scope_title_parts) if scope_title_parts else "MESA DE CONTROL INTEGRADA (GLOBAL)"

    # 1. Gráfico Donut de Distribución de Estados Operativos
    donut_svg_html = _build_svg_donut_chart(kpis)

    # 2. Gráfico de Barras por Especialista (compacto para el PDF)
    spec_list = data.get("por_especialista", [])
    max_spec_total = max([s["total"] for s in spec_list] + [1])
    spec_chart_bars = ""
    for s in spec_list:
        pct_w = (s["total"] / max_spec_total) * 100.0 if max_spec_total > 0 else 0.0
        short_name = "Sin asignar" if "sin asignar" in s["name"].lower() else s["name"]
        spec_chart_bars += f"""
        <div style="margin-bottom:7px">
            <div style="display:flex;justify-content:space-between;font-size:10.5px;margin-bottom:2px">
                <span style="font-weight:700;color:#1e293b">{short_name}</span>
                <span>
                    <strong>{s['total']}</strong> casos
                    (<span style="color:#10b981">{s['cerrados']} c.</span> / <span style="color:#0ea5e9">{s['en_curso']} act.</span> / <span style="color:#f59e0b">{s.get('en_espera', 0)} esp.</span>)
                    · <span style="color:#64748b">{s['avg_time_str']}</span>
                </span>
            </div>
            <div style="background:#e2e8f0;height:7px;border-radius:4px;overflow:hidden">
                <div style="width:{max(pct_w, 3 if s['total'] > 0 else 0):.1f}%;background:#2563eb;height:100%"></div>
            </div>
        </div>
        """

    # 3. Gráfico Comparativo por Categoría Técnica (barras apiladas + colapso de filas en 0)
    items_for_pdf = data.get("items", []) or []
    cat_breakdown = _pdf_breakdown(items_for_pdf, "category", "Infraestructura")
    cat_list = data.get("por_categoria", [])
    active_cats = [c for c in cat_list if (c.get("total") or 0) > 0]
    zero_cats = [c for c in cat_list if not (c.get("total") or 0) > 0]
    max_cat_total = max([c["total"] for c in active_cats] + [1])
    sum_cat_total = sum(c["total"] for c in active_cats)

    cat_chart_html = _pdf_segments_legend() if active_cats else ""
    cat_table_rows = ""
    if active_cats:
        for c in active_cats:
            c_col = PDF_CAT_COLORS.get(c["category"], "#2563eb")
            segs = _pdf_row_segments(c, cat_breakdown.get(c["category"]))
            cat_chart_html += _pdf_bar_row(
                label=c["category"],
                label_color="#0f172a",
                dot_color=c_col,
                segs=segs,
                total=c["total"],
                max_total=max_cat_total,
                sum_total=sum_cat_total,
            )
    else:
        cat_chart_html = '<div style="padding:12px;text-align:center;color:#64748b;font-size:10.5px">Ninguna categoría registra actividad en este período.</div>'

    if zero_cats:
        zero_chips = " · ".join(html_escape(c["category"]) for c in zero_cats)
        cat_chart_html += (
            f'<div style="margin-top:6px;padding:5px 8px;border:1px dashed #cbd5e1;border-radius:5px;font-size:9.5px;color:#64748b">'
            f'<strong>Sin actividad:</strong> {zero_chips}</div>'
        )

    for c in cat_list:
        cat_table_rows += f"""
        <tr>
            <td><strong>{c['category']}</strong></td>
            <td style="text-align:center;font-weight:700">{c['total']}</td>
            <td style="text-align:center">{c['total_items']}</td>
            <td style="text-align:center;color:#2563eb;font-weight:600">{c['total_escalamientos']}</td>
            <td style="text-align:center;color:#10b981">{c['cerrados']}</td>
            <td style="text-align:center;color:#0ea5e9">{c.get('en_curso', 0)}</td>
            <td style="text-align:center;color:#f59e0b">{c.get('en_espera', 0)}</td>
        </tr>
        """

    # 4. Gráfico Comparativo por IBP (Banco Participante, con General/Interno separado)
    ibp_breakdown = _pdf_breakdown(items_for_pdf, "ibp", GENERAL_IBP_LABEL)
    ibp_list = data.get("por_ibp", [])
    active_ibps = [i for i in ibp_list if (i.get("total") or 0) > 0]
    ibp_banks = [i for i in active_ibps if i["ibp"] != GENERAL_IBP_LABEL]
    ibp_general = next((i for i in active_ibps if i["ibp"] == GENERAL_IBP_LABEL), None)
    max_ibp_total = max([i["total"] for i in active_ibps] + [1])
    sum_ibp_total = sum(i["total"] for i in active_ibps)
    ibp_chart_html = ""
    ibp_table_rows = ""
    if active_ibps:
        ibp_chart_html = _pdf_segments_legend()
        for ibp_item in ibp_banks[:8]:
            segs = _pdf_row_segments(ibp_item, ibp_breakdown.get(ibp_item["ibp"]))
            ibp_chart_html += _pdf_bar_row(
                label=ibp_item["ibp"],
                label_color="#1b365d",
                dot_color=None,
                segs=segs,
                total=ibp_item["total"],
                max_total=max_ibp_total,
                sum_total=sum_ibp_total,
            )
        if ibp_general:
            segs = _pdf_row_segments(ibp_general, ibp_breakdown.get(ibp_general["ibp"]))
            ibp_chart_html += (
                '<div style="font-size:9px;color:#64748b;text-transform:uppercase;letter-spacing:0.4px;'
                'border-top:1px dashed #cbd5e1;padding-top:4px;margin:4px 0 3px 0">Sin IBP asociada</div>'
            )
            ibp_chart_html += _pdf_bar_row(
                label=ibp_general["ibp"],
                label_color="#64748b",
                dot_color=None,
                segs=segs,
                total=ibp_general["total"],
                max_total=max_ibp_total,
                sum_total=sum_ibp_total,
            )
        for ibp_item in ibp_list:
            ibp_table_rows += f"""
            <tr>
                <td><strong>{ibp_item['ibp']}</strong></td>
                <td style="text-align:center;font-weight:700">{ibp_item['total']}</td>
                <td style="text-align:center">{ibp_item['total_items']}</td>
                <td style="text-align:center;color:#2563eb;font-weight:600">{ibp_item['total_escalamientos']}</td>
                <td style="text-align:center;color:#10b981">{ibp_item['cerrados']}</td>
                <td style="text-align:center;color:#0ea5e9">{ibp_item.get('en_curso', 0)}</td>
                <td style="text-align:center;color:#f59e0b">{ibp_item.get('en_espera', 0)}</td>
            </tr>
            """
    else:
        ibp_chart_html = '<div style="padding:14px;text-align:center;color:#64748b;font-size:11px">Sin registros de IBP en este filtro.</div>'
        ibp_table_rows = '<tr><td colspan="7" style="text-align:center;color:#64748b">Sin registros de IBP en este filtro.</td></tr>'

    matrix_html = _pdf_ibp_category_matrix(data)

    # 5. Gráficos de Plataforma de Origen y Prioridad
    glpi_cnt = data["por_origen"].get("GLPI", 0)
    cu_cnt = data["por_origen"].get("ClickUp", 0)
    tot_orig = glpi_cnt + cu_cnt
    glpi_pct = (glpi_cnt / tot_orig * 100.0) if tot_orig > 0 else 0.0
    cu_pct = (cu_cnt / tot_orig * 100.0) if tot_orig > 0 else 0.0

    prio_colors = {"Crítica": "#ef4444", "Alta": "#f97316", "Media": "#3b82f6", "Baja": "#10b981"}
    prio_bars_html = ""
    for p_key in ["Crítica", "Alta", "Media", "Baja"]:
        p_cnt = data["por_prioridad"].get(p_key, 0)
        p_pct = (p_cnt / kpis["total_items"] * 100.0) if kpis["total_items"] > 0 else 0.0
        prio_bars_html += f"""
        <div style="margin-bottom:5px">
            <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px">
                <span style="font-weight:600;color:#334155">{p_key}</span>
                <span style="font-weight:700">{p_cnt} ({p_pct:.0f}%)</span>
            </div>
            <div style="background:#e2e8f0;height:5px;border-radius:3px;overflow:hidden">
                <div style="width:{p_pct:.1f}%;background:{prio_colors[p_key]};height:100%"></div>
            </div>
        </div>
        """

    # Tablas de Especialistas, Clientes y Escalamientos
    spec_rows = ""
    for s in data["por_especialista"]:
        spec_rows += f"""
        <tr>
            <td>
                <strong>{s['name']}</strong><br>
                <small style="color:#64748b">{s['role']}</small>
            </td>
            <td style="text-align:center;font-weight:700">{s['total']}</td>
            <td style="text-align:center;color:#10b981;font-weight:600">{s['cerrados']}</td>
            <td style="text-align:center;color:#0ea5e9;font-weight:600">{s['en_curso']}</td>
            <td style="text-align:center;color:#f59e0b;font-weight:600">{s.get('en_espera', 0)}</td>
            <td style="text-align:center">{s['pendientes']}</td>
            <td style="text-align:center">{s['avg_time_str']}</td>
        </tr>
        """

    client_rows = ""
    for c in data["por_cliente"][:10]:
        client_rows += f"""
        <tr>
            <td><strong>{c['client_name']}</strong></td>
            <td style="text-align:center">{c['client_type']}</td>
            <td style="text-align:center;font-weight:700">{c['total']}</td>
            <td style="text-align:center;color:#10b981">{c['cerrados']}</td>
            <td style="text-align:center;color:#0ea5e9">{c['activos']}</td>
            <td style="text-align:center;color:{'#ef4444' if c['criticos'] > 0 else '#64748b'}">{c['criticos']}</td>
        </tr>
        """

    esc_rows = ""
    for esc in data.get("escalations", [])[:25]:
        rec_text = (esc.get("recommendations") or esc.get("notes") or "Sin recomendaciones registradas").replace("\n", "<br>")
        esc_rows += f"""
        <tr>
            <td><strong>#{esc['id']} - {esc['title']}</strong></td>
            <td style="text-align:center"><span style="background:#eff6ff;color:#1d4ed8;padding:2px 6px;border-radius:4px;font-weight:700;font-size:9.5px">{esc['ibp']}</span></td>
            <td style="text-align:center"><span style="background:#f1f5f9;color:#0f172a;padding:2px 6px;border-radius:4px;font-weight:700;font-size:9.5px">{esc['category']}</span></td>
            <td style="text-align:center;font-size:10px">{esc['received_at_str']}</td>
            <td style="text-align:center;font-size:10px">{esc['escalated_at_str']}</td>
            <td style="font-size:10px;color:#334155">{rec_text}</td>
        </tr>
        """
    if not esc_rows:
        esc_rows = '<tr><td colspan="6" style="text-align:center;color:#64748b">No se registraron expedientes de escalamiento bajo este filtro.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Informe Ejecutivo - Soluciones SYCOM</title>
    <style>
        @page {{
            size: letter portrait;
            margin: 0mm !important;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            color: #0f172a;
            background: #f8fafc;
            margin: 0;
            padding: 24px;
            font-size: 11.5px;
            line-height: 1.4;
        }}
        .report-page {{
            max-width: 920px;
            margin: 0 auto;
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 28px 34px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.05);
        }}
        .no-print-bar {{
            max-width: 920px;
            margin: 0 auto 16px auto;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 12px 18px;
            background: #1b365d;
            border-radius: 8px;
            color: #ffffff;
        }}
        .btn-print {{
            background: #10b981;
            color: #ffffff;
            border: none;
            padding: 8px 18px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 13px;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }}
        .btn-print:hover {{ background: #059669; }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            border-bottom: 2px solid #1b365d;
            padding-bottom: 14px;
            margin-bottom: 16px;
        }}
        .company-title {{
            font-size: 18px;
            font-weight: 800;
            color: #1b365d;
            letter-spacing: -0.3px;
            text-transform: uppercase;
        }}
        .company-sub {{
            font-size: 12.5px;
            color: #475569;
            margin-top: 2px;
            font-weight: 600;
        }}
        .badge-period {{
            background: #eff6ff;
            border: 1px solid #bfdbfe;
            color: #1d4ed8;
            padding: 3px 9px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 10.5px;
            display: inline-block;
            margin-top: 6px;
            margin-right: 4px;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 8px;
            margin-bottom: 16px;
        }}
        .kpi-card {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 9px 6px;
            text-align: center;
            border-top: 3px solid #1b365d;
        }}
        .kpi-card.green {{ border-top-color: #10b981; }}
        .kpi-card.cyan {{ border-top-color: #0ea5e9; }}
        .kpi-card.blue {{ border-top-color: #2563eb; }}
        .kpi-card.yellow {{ border-top-color: #f59e0b; }}
        .kpi-lbl {{
            font-size: 8.5px;
            text-transform: uppercase;
            font-weight: 700;
            color: #64748b;
            letter-spacing: 0.3px;
        }}
        .kpi-val {{
            font-size: 19px;
            font-weight: 800;
            margin-top: 3px;
            color: #0f172a;
        }}
        .charts-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 14px;
        }}
        .chart-box {{
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 12px;
            background: #ffffff;
        }}
        .chart-box-title {{
            font-size: 11px;
            font-weight: 800;
            color: #1b365d;
            text-transform: uppercase;
            margin-bottom: 8px;
            border-bottom: 1px solid #f1f5f9;
            padding-bottom: 4px;
        }}
        .section-title {{
            font-size: 12.5px;
            font-weight: 700;
            color: #1b365d;
            text-transform: uppercase;
            letter-spacing: 0.2px;
            margin: 18px 0 8px 0;
            border-left: 3px solid #2563eb;
            padding-left: 8px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 14px;
            font-size: 10.5px;
        }}
        th {{
            background: #1b365d;
            color: #ffffff;
            font-weight: 700;
            padding: 7px 6px;
            text-align: left;
            font-size: 9.5px;
            text-transform: uppercase;
        }}
        td {{
            padding: 6px 6px;
            border-bottom: 1px solid #e2e8f0;
        }}
        tr:nth-child(even) td {{
            background: #f8fafc;
        }}
        .footer-signatures {{
            display: flex;
            justify-content: center;
            align-items: center;
            margin-top: 32px;
            padding-top: 20px;
            border-top: 1px dashed #cbd5e1;
        }}
        .sig-block {{
            text-align: center;
            min-width: 260px;
        }}
        .sig-line {{
            width: 240px;
            margin: 0 auto 8px auto;
            border-bottom: 1.5px solid #0f172a;
        }}
        @media print {{
            @page {{
                size: letter portrait;
                margin: 0mm !important;
            }}
            html, body {{
                background: #ffffff !important;
                margin: 0 !important;
                padding: 0 !important;
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }}
            .report-page {{
                border: none !important;
                box-shadow: none !important;
                margin: 0 auto !important;
                padding: 10mm 14mm 10mm 14mm !important;
                max-width: 100% !important;
                border-radius: 0 !important;
                box-sizing: border-box !important;
            }}
            .no-print-bar {{
                display: none !important;
            }}
            .kpi-grid, .charts-grid, .chart-box, .footer-signatures, table tr {{
                page-break-inside: avoid;
                break-inside: avoid;
            }}
        }}
    </style>
</head>
<body>
    <div class="no-print-bar">
        <div>
            <strong>Reporte Ejecutivo Oficial con KPIs y Gráficos</strong> · Listo para imprimir o guardar como PDF.
            <div style="font-size:11px;color:#94a3b8;margin-top:2px">
                Nota: En la ventana de impresión, desactiva la casilla <em>"Encabezados y pies de página"</em> y activa <em>"Gráficos de fondo"</em>.
            </div>
        </div>
        <div style="display:flex;gap:10px">
            <button class="btn-print" onclick="window.print()">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 6 2 18 2 18 9"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/></svg>
                <span>Imprimir / Guardar como PDF</span>
            </button>
        </div>
    </div>

    <div class="report-page">
        <div class="header">
            <div>
                <div class="company-title">Soluciones SYCOM</div>
                <div class="company-sub">Coordinación de Infraestructura y Base de Datos</div>
                <div>
                    <span class="badge-period">Período: {data['period_label']}</span>
                    <span class="badge-period">IBP: {ibp_filter_lbl}</span>
                    <span class="badge-period">Categoría: {cat_filter_lbl}</span>
                    <span class="badge-period">Especialista: {spec_filter_lbl}</span>
                </div>
            </div>
            <div style="text-align:right">
                <div style="font-size:10px;color:#64748b">INFORME EJECUTIVO DE GESTIÓN OPERATIVA</div>
                <div style="font-weight:700;font-size:11.5px;color:#1b365d;margin-top:2px">{dynamic_report_heading}</div>
                <div style="font-size:9.5px;color:#94a3b8;margin-top:4px">Emisión: {data['generated_at']}</div>
            </div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-lbl">Total Actividades</div>
                <div class="kpi-val">{kpis['total_items']}</div>
            </div>
            <div class="kpi-card green">
                <div class="kpi-lbl">Cerradas</div>
                <div class="kpi-val" style="color:#10b981">{kpis['total_cerrados']}</div>
            </div>
            <div class="kpi-card cyan">
                <div class="kpi-lbl">En Atención</div>
                <div class="kpi-val" style="color:#0ea5e9">{kpis['total_en_curso']}</div>
            </div>
            <div class="kpi-card yellow">
                <div class="kpi-lbl">En Espera</div>
                <div class="kpi-val" style="color:#f59e0b">{kpis.get('total_en_espera', 0)}</div>
            </div>
            <div class="kpi-card blue">
                <div class="kpi-lbl">Pendientes</div>
                <div class="kpi-val" style="color:#2563eb">{kpis['total_pendientes']}</div>
            </div>
            <div class="kpi-card blue">
                <div class="kpi-lbl">Escalamientos</div>
                <div class="kpi-val" style="color:#1b365d">{kpis.get('total_escalamientos', 0)}</div>
            </div>
        </div>

        <div style="font-size:11px;color:#475569;margin-bottom:14px;background:#f1f5f9;padding:7px 12px;border-radius:6px;display:flex;justify-content:space-between">
            <span><strong>Tiempo Promedio de Resolución:</strong> {kpis['avg_resolution_str']}</span>
            <span><strong>Total Gestionadas:</strong> {kpis['total_items']} · <strong>Cerradas:</strong> {kpis['total_cerrados']} · <strong>En Espera:</strong> {kpis.get('total_en_espera', 0)}</span>
        </div>

        <div class="section-title">1. Panel Gráfico de Indicadores Clave (KPIs, IBP y Categorías)</div>
        <div class="charts-grid">
            <div class="chart-box">
                <div class="chart-box-title">Distribución de Estados Operativos</div>
                {donut_svg_html}
            </div>
            <div class="chart-box">
                <div class="chart-box-title">Carga Operativa y Rendimiento por Especialista</div>
                {spec_chart_bars}
            </div>
        </div>

        <div class="charts-grid">
            <div class="chart-box">
                <div class="chart-box-title">Gráfico por Categoría Técnica (Volumen Operativo)</div>
                {cat_chart_html}
            </div>
            <div class="chart-box">
                <div class="chart-box-title">Gráfico por IBP / Banco Participante (Volumen Operativo)</div>
                {ibp_chart_html}
                <div style="margin-top:10px;padding-top:8px;border-top:1px solid #f1f5f9">
                    <div style="font-size:10px;font-weight:700;color:#1b365d;margin-bottom:4px">ORIGEN Y PRIORIDAD OPERATIVA</div>
                    <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:4px">
                        <span><strong>GLPI:</strong> {glpi_cnt} ({glpi_pct:.0f}%)</span>
                        <span><strong>ClickUp:</strong> {cu_cnt} ({cu_pct:.0f}%)</span>
                    </div>
                    {prio_bars_html}
                </div>
            </div>
        </div>

        <div class="chart-box" style="margin-bottom:14px">
            <div class="chart-box-title">Matriz IBP × Categoría Técnica (Mapa de Calor)</div>
            {matrix_html}
        </div>

        <div class="section-title">2. Desglose Ejecutivo por IBP (Banco Participante)</div>
        <table>
            <thead>
                <tr>
                    <th>IBP / Entidad Bancaria</th>
                    <th style="text-align:center">Volumen Total</th>
                    <th style="text-align:center">Actividades</th>
                    <th style="text-align:center">Escalamientos</th>
                    <th style="text-align:center">Cerrados</th>
                    <th style="text-align:center">En Curso</th>
                    <th style="text-align:center">En Espera</th>
                </tr>
            </thead>
            <tbody>
                {ibp_table_rows}
            </tbody>
        </table>

        <div class="section-title">3. Desglose Ejecutivo por Categoría Técnica</div>
        <table>
            <thead>
                <tr>
                    <th>Categoría Técnica</th>
                    <th style="text-align:center">Volumen Total</th>
                    <th style="text-align:center">Actividades</th>
                    <th style="text-align:center">Escalamientos</th>
                    <th style="text-align:center">Cerrados</th>
                    <th style="text-align:center">En Curso</th>
                    <th style="text-align:center">En Espera</th>
                </tr>
            </thead>
            <tbody>
                {cat_table_rows}
            </tbody>
        </table>

        <div class="section-title">4. Rendimiento y Carga Operativa por Especialista</div>
        <table>
            <thead>
                <tr>
                    <th>Especialista</th>
                    <th style="text-align:center">Asignadas</th>
                    <th style="text-align:center">Cerradas</th>
                    <th style="text-align:center">En Curso</th>
                    <th style="text-align:center">En Espera</th>
                    <th style="text-align:center">Pendientes</th>
                    <th style="text-align:center">T. Promedio</th>
                </tr>
            </thead>
            <tbody>
                {spec_rows}
            </tbody>
        </table>

        <div class="section-title">5. Expedientes de Escalamientos y Recomendaciones Técnicas</div>
        <table>
            <thead>
                <tr>
                    <th>Expediente / Asunto</th>
                    <th style="text-align:center">IBP</th>
                    <th style="text-align:center">Categoría</th>
                    <th style="text-align:center">Recibido Sycom</th>
                    <th style="text-align:center">Escalado Infra</th>
                    <th>Recomendaciones / Notas</th>
                </tr>
            </thead>
            <tbody>
                {esc_rows}
            </tbody>
        </table>

        <div class="section-title">6. Volumen de Actividad por Cliente / Entidad</div>
        <table>
            <thead>
                <tr>
                    <th>Cliente / Espacio</th>
                    <th style="text-align:center">Tipo</th>
                    <th style="text-align:center">Total</th>
                    <th style="text-align:center">Resueltos</th>
                    <th style="text-align:center">Activos</th>
                    <th style="text-align:center">Críticos</th>
                </tr>
            </thead>
            <tbody>
                {client_rows}
            </tbody>
        </table>

        <div class="footer-signatures">
            <div class="sig-block">
                <div class="sig-line"></div>
                <strong>Adonis Lozada Figueroa</strong><br>
                <small style="color:#64748b">Coordinador de Infraestructura y BD<br>Soluciones SYCOM</small>
            </div>
        </div>

        <div style="margin-top:28px;border-top:1px solid #e2e8f0;padding-top:10px;display:flex;justify-content:space-between;align-items:center;font-size:9.5px;color:#94a3b8">
            <span>Soluciones SYCOM · Gestión de Infraestructura y Base de Datos</span>
            <span>Documento Confidencial · Gestión Operativa</span>
        </div>
    </div>
</body>
</html>
"""
