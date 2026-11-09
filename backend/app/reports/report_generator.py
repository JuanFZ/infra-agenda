"""
Módulo de Generación de Reportes Ejecutivos (M9)
Agenda de Infraestructura y Base de Datos - Sycom
Soporta exportación en Excel (.xlsx) con openpyxl y vista ejecutiva imprimible en PDF/HTML.
"""

import io
import datetime
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import or_, and_, func

from app.models import Item
from app.notifications.telegram_notifier import CARACAS_TZ, format_caracas_time

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


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
    specialist_filter: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Obtiene y procesa las métricas consolidadas para reportes ejecutivos.
    """
    query = db.query(Item)

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

    if clean_start:
        query = query.filter(
            or_(
                Item.created_at >= clean_start,
                Item.closed_at >= clean_start,
                Item.updated_at >= clean_start
            )
        )
    if clean_end:
        query = query.filter(Item.created_at <= clean_end)

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

    items: List[Item] = query.order_by(Item.created_at.desc()).all()

    total_items = len(items)
    cerrados = [it for it in items if it.status == "cerrado"]
    en_curso = [it for it in items if it.status == "curso"]
    pendientes = [it for it in items if it.status == "pendiente"]
    en_espera = [it for it in items if it.status == "espera"]

    # Cálculo de SLA
    dentro_sla = [it for it in items if not (it.is_overdue or it.semaphore == "rojo")]
    fuera_sla = [it for it in items if it.is_overdue or it.semaphore == "rojo"]

    cerrados_ok = [it for it in cerrados if not (it.is_overdue or it.semaphore == "rojo")]
    cerrados_fuera = [it for it in cerrados if (it.is_overdue or it.semaphore == "rojo")]
    en_curso_ok = [it for it in en_curso if not (it.is_overdue or it.semaphore == "rojo")]
    en_curso_fuera = [it for it in en_curso if (it.is_overdue or it.semaphore == "rojo")]
    pendientes_ok = [it for it in pendientes if not (it.is_overdue or it.semaphore == "rojo")]
    pendientes_fuera = [it for it in pendientes if (it.is_overdue or it.semaphore == "rojo")]
    en_espera_ok = [it for it in en_espera if not (it.is_overdue or it.semaphore == "rojo")]
    en_espera_fuera = [it for it in en_espera if (it.is_overdue or it.semaphore == "rojo")]

    cumplimiento_general = 100.0
    if cerrados:
        cerrados_ok = [it for it in cerrados if not (it.is_overdue or it.semaphore == "rojo")]
        cumplimiento_general = round((len(cerrados_ok) / len(cerrados)) * 100.0, 1)
    elif total_items > 0:
        cumplimiento_general = round((len(dentro_sla) / total_items) * 100.0, 1)

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
        {"name": "Sin asignar / Mesa TI", "role": "Bolsa Disponible", "keys": ["sin asignar", "técnico glpi"]}
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
        s_fuera_sla = [it for it in matched if it.is_overdue or it.semaphore == "rojo"]

        s_cumplimiento = 100.0
        if s_cerrados:
            s_ok = [it for it in s_cerrados if not (it.is_overdue or it.semaphore == "rojo")]
            s_cumplimiento = round((len(s_ok) / len(s_cerrados)) * 100.0, 1)
        elif s_total > 0:
            s_ok = [it for it in matched if not (it.is_overdue or it.semaphore == "rojo")]
            s_cumplimiento = round((len(s_ok) / s_total) * 100.0, 1)

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
            "fuera_sla": len(s_fuera_sla),
            "cumplimiento_pct": s_cumplimiento,
            "avg_time_str": format_duration_seconds(s_avg_dur)
        })

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
        c_ok = [it for it in c_items if not (it.is_overdue or it.semaphore == "rojo")]
        c_sla = round((len(c_ok) / len(c_items)) * 100.0, 1) if c_items else 100.0
        por_cliente.append({
            "client_name": c_name,
            "client_type": c_items[0].client_type if c_items else "Interno",
            "total": len(c_items),
            "cerrados": len(c_cerrados),
            "activos": len(c_activos),
            "criticos": len(c_criticos),
            "cumplimiento_pct": c_sla
        })

    # Detalle de Items
    items_detail = []
    for it in items:
        ref_time = it.closed_at if it.status == "cerrado" and it.closed_at else datetime.datetime.utcnow()
        dur_str = format_duration_seconds((ref_time - it.created_at).total_seconds()) if it.created_at else "0m"

        items_detail.append({
            "id": it.id,
            "source": it.source,
            "external_id": it.external_id,
            "url": it.url or "",
            "title": it.title or "",
            "client_name": it.client_name or "Interno",
            "client_type": it.client_type or "Interno",
            "assignee_name": it.assignee_name or "Sin asignar",
            "priority": it.priority or "Media",
            "status": it.status or "pendiente",
            "status_origin": it.status_origin or it.status or "",
            "sla_percent": it.sla_percent,
            "semaphore": it.semaphore,
            "is_overdue": it.is_overdue,
            "duration_str": dur_str,
            "created_at_str": format_caracas_time(it.created_at) if it.created_at else "—",
            "assigned_at_str": format_caracas_time(it.assigned_at) if it.assigned_at else "—",
            "closed_at_str": format_caracas_time(it.closed_at) if it.closed_at else "—",
            "due_at_str": format_caracas_time(it.due_at) if it.due_at else "—",
        })

    # Determinación de etiquetas de fechas
    now_caracas = datetime.datetime.now(CARACAS_TZ)
    if not clean_start and not clean_end:
        period_str = f"Mes en curso ({now_caracas.strftime('%B %Y').capitalize()})"
    elif clean_start and clean_end:
        period_str = f"{format_caracas_time(clean_start, '%d-%m-%Y')} al {format_caracas_time(clean_end, '%d-%m-%Y')}"
    elif clean_start:
        period_str = f"Desde {format_caracas_time(clean_start, '%d-%m-%Y')}"
    else:
        period_str = f"Hasta {format_caracas_time(clean_end, '%d-%m-%Y')}"

    return {
        "generated_at": now_caracas.strftime("%d-%m-%Y %I:%M %p"),
        "period_label": period_str,
        "kpis": {
            "total_items": total_items,
            "total_cerrados": len(cerrados),
            "total_cerrados_ok": len(cerrados_ok),
            "total_cerrados_fuera": len(cerrados_fuera),
            "total_en_curso": len(en_curso),
            "total_en_curso_ok": len(en_curso_ok),
            "total_pendientes": len(pendientes),
            "total_pendientes_ok": len(pendientes_ok),
            "total_en_espera": len(en_espera),
            "total_en_espera_ok": len(en_espera_ok),
            "total_dentro_sla": len(dentro_sla),
            "total_fuera_sla": len(fuera_sla),
            "cumplimiento_general": cumplimiento_general,
            "avg_resolution_str": format_duration_seconds(avg_resolution_seconds)
        },
        "por_origen": por_origen,
        "por_prioridad": por_prioridad,
        "por_especialista": por_especialista,
        "por_cliente": por_cliente,
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
    ws_resumen["A2"] = f"INFORME EJECUTIVO DE GESTIÓN OPERATIVA Y CUMPLIMIENTO DE SLA · {data['period_label'].upper()}"
    ws_resumen["A2"].font = font_subtitle
    ws_resumen["A2"].fill = fill_subheader
    ws_resumen["A2"].alignment = align_center

    ws_resumen["A4"] = f"Fecha de Emisión: {data['generated_at']} (Hora Legal de Caracas, UTC-4)"
    ws_resumen["A4"].font = Font(name="Calibri", size=9.5, italic=True, color="64748B")

    # Tarjetas KPI Principales (Fila 6 y 7)
    kpis = data["kpis"]
    kpi_cards = [
        ("A", "B", "TOTAL ASUNTOS", str(kpis["total_items"]), "1B365D"),
        ("C", "C", "CERRADOS", str(kpis["total_cerrados"]), "15803D"),
        ("D", "D", "EN ATENCIÓN", str(kpis["total_en_curso"]), "0891B2"),
        ("E", "E", "PENDIENTES", str(kpis["total_pendientes"]), "2563EB"),
        ("F", "F", "EN ESPERA", str(kpis["total_en_espera"]), "D97706"),
        ("G", "G", "CUMPLIMIENTO SLA", f"{kpis['cumplimiento_general']}%", "15803D" if kpis['cumplimiento_general'] >= 90 else "D97706")
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

    headers_prio = ["Nivel de Prioridad", "Cantidad de Casos", "% del Total", "Plazo de Atención SLA"]
    prio_slas = {
        "Crítica": "Inmediato (< 8h)",
        "Alta": "Prioritario (24h)",
        "Media": "Estándar (48h)",
        "Baja": "Planificado (72h)"
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
        ws_resumen.cell(row=row_cur, column=4, value=prio_slas.get(prio, "—")).alignment = align_center
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

    ws_spec.merge_cells("A1:I1")
    ws_spec["A1"] = "RENDIMIENTO Y CARGA DE TRABAJO POR ESPECIALISTA"
    ws_spec["A1"].font = font_title
    ws_spec["A1"].fill = fill_header
    ws_spec["A1"].alignment = align_center

    ws_spec.merge_cells("A2:I2")
    ws_spec["A2"] = f"Auditoría Individual de Productividad, Cumplimiento de SLA y Casos Atendidos · {data['period_label']}"
    ws_spec["A2"].font = font_subtitle
    ws_spec["A2"].fill = fill_subheader
    ws_spec["A2"].alignment = align_center

    headers_spec = [
        "Especialista", "Rol Operativo", "Total Asignadas", 
        "Cerradas", "En Atención", "Pendientes", 
        "Fuera de SLA", "% Cumplimiento SLA", "Tiempo Promedio"
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
        ws_spec.cell(row=r_idx, column=6, value=s["pendientes"]).alignment = align_center
        ws_spec.cell(row=r_idx, column=7, value=s["fuera_sla"]).alignment = align_center
        
        c_sla = ws_spec.cell(row=r_idx, column=8, value=f"{s['cumplimiento_pct']:.1f}%")
        c_sla.alignment = align_center
        if s["cumplimiento_pct"] >= 90:
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="15803D")
        else:
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="D97706")

        ws_spec.cell(row=r_idx, column=9, value=s["avg_time_str"]).alignment = align_center

        for c in range(1, 10):
            cell = ws_spec.cell(row=r_idx, column=c)
            cell.border = border_cell
            if c != 8:
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

    ws_client.merge_cells("A1:G1")
    ws_client["A1"] = "VOLUMEN DE SERVICIOS Y NIVELES DE SLA POR CLIENTE / ENTIDAD"
    ws_client["A1"].font = font_title
    ws_client["A1"].fill = fill_header
    ws_client["A1"].alignment = align_center

    ws_client.merge_cells("A2:G2")
    ws_client["A2"] = f"Consolidación por Cliente / Espacio Operativo · {data['period_label']}"
    ws_client["A2"].font = font_subtitle
    ws_client["A2"].fill = fill_subheader
    ws_client["A2"].alignment = align_center

    headers_client = [
        "Cliente / Espacio", "Tipo de Entidad", "Total Requerimientos",
        "Resueltos", "En Atención Activa", "Casos Críticos", "% Cumplimiento SLA"
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
        
        c_sla = ws_client.cell(row=r_idx, column=7, value=f"{c['cumplimiento_pct']:.1f}%")
        c_sla.alignment = align_center
        if c["cumplimiento_pct"] >= 90:
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="15803D")
        else:
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="D97706")

        for col_i in range(1, 8):
            cell = ws_client.cell(row=r_idx, column=col_i)
            cell.border = border_cell
            if col_i != 7:
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
        "Duración Real", "SLA %", "Fecha Creación", "Fecha Asignación", 
        "Fecha Cierre", "Enlace Nativo"
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
        ws_items.cell(row=r_idx, column=9, value=it["duration_str"]).alignment = align_center
        
        c_sla = ws_items.cell(row=r_idx, column=10, value=f"{it['sla_percent']:.1f}%")
        c_sla.alignment = align_center
        if it["semaphore"] == "rojo":
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="DC2626")
        elif it["semaphore"] == "amarillo":
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="D97706")
        else:
            c_sla.font = Font(name="Calibri", size=10, bold=True, color="15803D")

        ws_items.cell(row=r_idx, column=11, value=it["created_at_str"]).alignment = align_center
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
            if col_i not in (10, 14):
                cell.font = font_body
            if row_fill.fill_type:
                cell.fill = row_fill

    # Anchos para Hoja Detalle
    widths = {1: 8, 2: 11, 3: 13, 4: 42, 5: 24, 6: 28, 7: 12, 8: 16, 9: 14, 10: 12, 11: 19, 12: 19, 13: 19, 14: 30}
    for col_idx, w in widths.items():
        col_letter = get_column_letter(col_idx)
        ws_items.column_dimensions[col_letter].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def generate_executive_html(data: Dict[str, Any]) -> str:
    """
    Genera un informe visual ejecutivo imprimible en PDF con formato de alta calidad.
    """
    kpis = data["kpis"]
    spec_rows = ""
    for s in data["por_especialista"]:
        pct = s["cumplimiento_pct"]
        bar_color = "#10b981" if pct >= 90 else ("#f59e0b" if pct >= 75 else "#ef4444")
        spec_rows += f"""
        <tr>
            <td>
                <strong>{s['name']}</strong><br>
                <small style="color:#64748b">{s['role']}</small>
            </td>
            <td style="text-align:center">{s['total']}</td>
            <td style="text-align:center;color:#10b981;font-weight:600">{s['cerrados']}</td>
            <td style="text-align:center;color:#0ea5e9;font-weight:600">{s['en_curso']}</td>
            <td style="text-align:center">{s['pendientes']}</td>
            <td style="text-align:center;color:{'#ef4444' if s['fuera_sla'] > 0 else '#64748b'}">{s['fuera_sla']}</td>
            <td style="text-align:center">
                <div style="font-weight:700;color:{bar_color}">{pct:.1f}%</div>
                <div style="background:#e2e8f0;border-radius:4px;height:5px;width:100%;margin-top:3px;overflow:hidden">
                    <div style="background:{bar_color};height:100%;width:{min(100, pct)}%"></div>
                </div>
            </td>
            <td style="text-align:center">{s['avg_time_str']}</td>
        </tr>
        """

    client_rows = ""
    for c in data["por_cliente"][:10]:
        client_rows += f"""
        <tr>
            <td><strong>{c['client_name']}</strong></td>
            <td style="text-align:center">{c['client_type']}</td>
            <td style="text-align:center">{c['total']}</td>
            <td style="text-align:center;color:#10b981">{c['cerrados']}</td>
            <td style="text-align:center;color:#0ea5e9">{c['activos']}</td>
            <td style="text-align:center;color:{'#ef4444' if c['criticos'] > 0 else '#64748b'}">{c['criticos']}</td>
            <td style="text-align:center;font-weight:600;color:{'#10b981' if c['cumplimiento_pct'] >= 90 else '#f59e0b'}">{c['cumplimiento_pct']:.1f}%</td>
        </tr>
        """

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
            max-width: 900px;
            margin: 0 auto;
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 32px 36px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.05);
        }}
        .no-print-bar {{
            max-width: 900px;
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
            padding-bottom: 16px;
            margin-bottom: 20px;
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
            padding: 4px 10px;
            border-radius: 6px;
            font-weight: 700;
            font-size: 11px;
            display: inline-block;
            margin-top: 6px;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(6, 1fr);
            gap: 10px;
            margin-bottom: 24px;
        }}
        .kpi-card {{
            background: #f8fafc;
            border: 1px solid #e2e8f0;
            border-radius: 8px;
            padding: 10px 8px;
            text-align: center;
            border-top: 3px solid #1b365d;
        }}
        .kpi-card.green {{ border-top-color: #10b981; }}
        .kpi-card.cyan {{ border-top-color: #0ea5e9; }}
        .kpi-card.blue {{ border-top-color: #2563eb; }}
        .kpi-card.yellow {{ border-top-color: #f59e0b; }}
        .kpi-lbl {{
            font-size: 9px;
            text-transform: uppercase;
            font-weight: 700;
            color: #64748b;
            letter-spacing: 0.3px;
        }}
        .kpi-val {{
            font-size: 20px;
            font-weight: 800;
            margin-top: 4px;
            color: #0f172a;
        }}
        .section-title {{
            font-size: 13px;
            font-weight: 700;
            color: #1b365d;
            text-transform: uppercase;
            letter-spacing: 0.2px;
            margin: 20px 0 10px 0;
            border-left: 3px solid #2563eb;
            padding-left: 8px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 16px;
            font-size: 11px;
        }}
        th {{
            background: #1b365d;
            color: #ffffff;
            font-weight: 700;
            padding: 8px 6px;
            text-align: left;
            font-size: 10px;
            text-transform: uppercase;
        }}
        td {{
            padding: 7px 6px;
            border-bottom: 1px solid #e2e8f0;
        }}
        tr:nth-child(even) td {{
            background: #f8fafc;
        }}
        .footer-signatures {{
            display: flex;
            justify-content: center;
            align-items: center;
            margin-top: 40px;
            padding-top: 24px;
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
                padding: 12mm 15mm 12mm 15mm !important;
                max-width: 100% !important;
                border-radius: 0 !important;
                box-sizing: border-box !important;
            }}
            .no-print-bar {{
                display: none !important;
            }}
            .kpi-grid, .breakdown-grid, .footer-signatures, table tr {{
                page-break-inside: avoid;
                break-inside: avoid;
            }}
        }}
    </style>
</head>
<body>
    <div class="no-print-bar">
        <div>
            <strong>Reporte Ejecutivo Oficial</strong> · Listo para imprimir o guardar como PDF.
            <div style="font-size:11px;color:#94a3b8;margin-top:2px">
                Nota: En la ventana de impresión, desactiva la casilla <em>"Encabezados y pies de página"</em> para eliminar cualquier URL residual del navegador.
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
                <div class="badge-period">Período: {data['period_label']}</div>
            </div>
            <div style="text-align:right">
                <div style="font-size:10px;color:#64748b">INFORME DE GESTIÓN MENSUAL</div>
                <div style="font-weight:700;font-size:12px;color:#1b365d;margin-top:2px">MESA DE CONTROL INTEGRADA</div>
                <div style="font-size:9.5px;color:#94a3b8;margin-top:4px">Emisión: {data['generated_at']}</div>
            </div>
        </div>

        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-lbl">Total Casos</div>
                <div class="kpi-val">{kpis['total_items']}</div>
            </div>
            <div class="kpi-card green">
                <div class="kpi-lbl">Cerrados</div>
                <div class="kpi-val" style="color:#10b981">{kpis['total_cerrados']}</div>
            </div>
            <div class="kpi-card cyan">
                <div class="kpi-lbl">En Atención</div>
                <div class="kpi-val" style="color:#0ea5e9">{kpis['total_en_curso']}</div>
            </div>
            <div class="kpi-card blue">
                <div class="kpi-lbl">Pendientes</div>
                <div class="kpi-val" style="color:#2563eb">{kpis['total_pendientes']}</div>
            </div>
            <div class="kpi-card yellow">
                <div class="kpi-lbl">En Espera</div>
                <div class="kpi-val" style="color:#f59e0b">{kpis['total_en_espera']}</div>
            </div>
            <div class="kpi-card green">
                <div class="kpi-lbl">Cumplimiento SLA</div>
                <div class="kpi-val" style="color:{'#10b981' if kpis['cumplimiento_general'] >= 90 else '#f59e0b'}">{kpis['cumplimiento_general']}%</div>
            </div>
        </div>

        <div style="font-size:11.5px;color:#475569;margin-bottom:14px;background:#f1f5f9;padding:8px 12px;border-radius:6px;display:flex;justify-content:space-between">
            <span><strong>Tiempo Promedio de Resolución:</strong> {kpis['avg_resolution_str']}</span>
            <span><strong>Fuera de SLA:</strong> {kpis['total_fuera_sla']} casos · <strong>Dentro de SLA:</strong> {kpis['total_dentro_sla']} casos</span>
        </div>

        <div class="section-title">1. Rendimiento y Carga Operativa por Especialista</div>
        <table>
            <thead>
                <tr>
                    <th>Especialista</th>
                    <th style="text-align:center">Asignadas</th>
                    <th style="text-align:center">Cerradas</th>
                    <th style="text-align:center">En Curso</th>
                    <th style="text-align:center">Pendientes</th>
                    <th style="text-align:center">Fuera SLA</th>
                    <th style="text-align:center">SLA %</th>
                    <th style="text-align:center">T. Promedio</th>
                </tr>
            </thead>
            <tbody>
                {spec_rows}
            </tbody>
        </table>

        <div class="section-title">2. Volumen de Actividad y Cumplimiento por Cliente / Entidad</div>
        <table>
            <thead>
                <tr>
                    <th>Cliente / Espacio</th>
                    <th style="text-align:center">Tipo</th>
                    <th style="text-align:center">Total</th>
                    <th style="text-align:center">Resueltos</th>
                    <th style="text-align:center">Activos</th>
                    <th style="text-align:center">Críticos</th>
                    <th style="text-align:center">Cumplimiento SLA</th>
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
            <span>Documento Confidencial · Cumplimiento de SLA</span>
        </div>
    </div>
</body>
</html>
"""
