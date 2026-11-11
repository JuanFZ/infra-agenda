import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Float
from .database import Base

class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    source = Column(String(20), index=True)          # 'GLPI' o 'ClickUp'
    external_id = Column(String(100), index=True)   # ID en GLPI o ClickUp
    url = Column(String(500), nullable=True)        # Enlace directo
    title = Column(String(500))                     # Asunto / Nombre
    category = Column(String(100), default="General") # BD, Servidores, etc.
    client_name = Column(String(150), default="Interno")
    project_name = Column(String(150), nullable=True) # SIMF, SGLPAR, Sycom, MicroFilms Center, etc.
    client_type = Column(String(50), default="Interno") # Interno / Externo
    assignee_name = Column(String(150), default="Sin asignar")
    priority = Column(String(50), default="Media")  # Crítica, Alta, Media, Baja
    status = Column(String(50), default="pendiente") # pendiente, curso, espera, cerrado
    status_origin = Column(String(100), nullable=True) # Estado original en origen
    description = Column(Text, nullable=True)       # Detalle o alcance técnico de la actividad
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    received_at = Column(DateTime, nullable=True)   # Fecha de Recibimiento en Sycom
    escalated_at = Column(DateTime, nullable=True)  # Fecha de Escalada al equipo Infra & BD
    assigned_at = Column(DateTime, nullable=True)
    due_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)
    synced_at = Column(DateTime, default=datetime.datetime.utcnow)
    escalation_id = Column(Integer, nullable=True)  # Vínculo opcional con escalation_documents.id
    recommendations = Column(Text, nullable=True)   # Recomendaciones registradas al cerrar o editar

    @property
    def attention_start(self) -> datetime.datetime | None:
        """Punto de inicio para cálculo cronológico: escalated_at o received_at o created_at."""
        return self.escalated_at or self.received_at or self.created_at

    @property
    def elapsed_seconds(self) -> float:
        """Segundos transcurridos desde el inicio de la actividad hasta su cierre o tiempo presente."""
        start = self.attention_start
        if not start:
            return 0.0
        ref = self.closed_at if self.status == "cerrado" and self.closed_at else datetime.datetime.utcnow()
        return max(0.0, (ref - start).total_seconds())

    @property
    def duration_human(self) -> str:
        """Duración formateada cronológicamente (ej. '2h 15m' o '3d 4h')."""
        secs = int(self.elapsed_seconds)
        if secs < 60:
            return "< 1m"
        mins = secs // 60
        hours = mins // 60
        days = hours // 24
        if days > 0:
            rem_hours = hours % 24
            return f"{days}d {rem_hours}h"
        if hours > 0:
            rem_mins = mins % 60
            return f"{hours}h {rem_mins}m"
        return f"{mins}m"

    @property
    def time_attention_label(self) -> str:
        """Tiempo de atención cronológico puro: 'Duró 2h 15m' o 'Lleva activa 3d 4h'."""
        dur = self.duration_human
        if self.status == "cerrado":
            return f"Duró {dur}"
        return f"Lleva activa {dur}"

    @property
    def semaphore(self) -> str:
        """Semáforo operativo de atención según plazo asignado."""
        if not self.due_at or not self.created_at:
            return "verde"
        total = (self.due_at - self.created_at).total_seconds()
        if total <= 0:
            return "rojo"
        elapsed = ((self.closed_at or datetime.datetime.utcnow()) - self.created_at).total_seconds()
        pct = (elapsed / total) * 100.0
        if pct < 75.0:
            return "verde"
        elif pct < 100.0:
            return "amarillo"
        return "rojo"


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    source = Column(String(50))
    status = Column(String(50)) # 'OK', 'ERROR'
    records_synced = Column(Integer, default=0)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    item_id = Column(Integer, index=True)
    semaphore = Column(String(20)) # 'amarillo', 'rojo'
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    full_name = Column(String(150), unique=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=True) # j.fernandez, a.lozada, etc.
    email = Column(String(150), unique=True, index=True, nullable=True) # j.fernandez@sycom.com.ve
    telegram_username = Column(String(100), nullable=True) # @Juuan8, etc.
    role = Column(String(50), default="especialista") # 'coordinador', 'especialista', 'analista'
    password_hash = Column(String(200), nullable=True)
    avatar_data = Column(Text, nullable=True) # Data URI base64 de la foto de perfil o URL
    phone = Column(String(50), nullable=True) # Teléfono de contacto
    job_title = Column(String(100), nullable=True) # Cargo o puesto
    bio = Column(String(255), nullable=True) # Resumen o biografía corta
    is_active = Column(Integer, default=1)
    last_login = Column(DateTime, nullable=True)


class GeneratedReport(Base):
    __tablename__ = "generated_reports"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    title = Column(String(200))
    period_label = Column(String(100))
    start_date = Column(String(50), nullable=True)
    end_date = Column(String(50), nullable=True)
    specialist_filter = Column(String(100), nullable=True)
    generated_by = Column(String(150), default="Adonis Lozada")
    total_items = Column(Integer, default=0)
    total_closed = Column(Integer, default=0)
    sla_percent = Column(Float, nullable=True, default=None)
    avg_resolution_time = Column(String(50), default="0m")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class EscalationDocument(Base):
    __tablename__ = "escalation_documents"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    title = Column(String(250), nullable=False)               # ej. "SIMF-ES-5260 88920 Venezolano"
    category = Column(String(100), default="Servicios BD")   # CPU, RAM, PostgreSQL, Servicios BD, Discos, Procesos
    doc_url = Column(String(1000), nullable=True)             # Enlace a Google Docs o Drive
    original_filename = Column(String(250), nullable=True)
    stored_filename = Column(String(250), nullable=True)
    file_size_bytes = Column(Integer, default=0)
    file_size_formatted = Column(String(50), default="Google Docs")
    uploaded_by = Column(String(150), default="Adonis Lozada")
    uploaded_by_role = Column(String(50), default="Coordinador")
    notes = Column(Text, nullable=True)
    recommendations = Column(Text, nullable=True)
    ibp_id = Column(Integer, nullable=True)                   # Vínculo a ibps.id
    ibp = Column(String(100), nullable=True)                  # Identificación Bancaria del Participante (ej. "0134 - BANESCO")
    received_at = Column(DateTime, nullable=True)             # Fecha de llegada del escalamiento a Sycom
    escalated_at = Column(DateTime, nullable=True)            # Fecha de escalada al equipo Infra & BD
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class IBP(Base):
    __tablename__ = "ibps"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(150), unique=True, index=True, nullable=False) # ej. "Banesco", "Banco Mercantil"
    code = Column(String(50), unique=True, index=True, nullable=True)   # ej. "BANESCO", "MERCANTIL"
    description = Column(String(300), nullable=True)
    environment = Column(String(50), default="Producción")
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)




