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
    client_type = Column(String(50), default="Interno") # Interno / Externo
    assignee_name = Column(String(150), default="Sin asignar")
    priority = Column(String(50), default="Media")  # Crítica, Alta, Media, Baja
    status = Column(String(50), default="pendiente") # pendiente, curso, espera, cerrado
    status_origin = Column(String(100), nullable=True) # Estado original en origen
    
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    assigned_at = Column(DateTime, nullable=True)
    due_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)
    synced_at = Column(DateTime, default=datetime.datetime.utcnow)

    @property
    def is_overdue(self) -> bool:
        if self.status == "cerrado" or not self.due_at:
            return False
        return datetime.datetime.utcnow() > self.due_at

    @property
    def sla_percent(self) -> float:
        if not self.due_at or not self.created_at:
            return 0.0
        total_time = (self.due_at - self.created_at).total_seconds()
        if total_time <= 0:
            return 100.0
        reference_time = self.closed_at if self.status == "cerrado" and self.closed_at else datetime.datetime.utcnow()
        elapsed = (reference_time - self.created_at).total_seconds()
        return round(max(0.0, (elapsed / total_time) * 100.0), 1)

    @property
    def semaphore(self) -> str:
        pct = self.sla_percent
        if pct < 75.0:
            return "verde"
        elif pct < 100.0:
            return "amarillo"
        else:
            return "rojo"

    @property
    def elapsed_seconds(self) -> float:
        if not self.created_at:
            return 0.0
        ref = self.closed_at if self.status == "cerrado" and self.closed_at else datetime.datetime.utcnow()
        return max(0.0, (ref - self.created_at).total_seconds())

    @property
    def duration_human(self) -> str:
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
    def time_sla_label(self) -> str:
        dur = self.duration_human
        if self.status == "cerrado":
            return f"Duró {dur}"
        elif self.status == "pendiente":
            return f"Sin atender: {dur}"
        elif self.status == "curso":
            return f"En atención: {dur}"
        elif self.status == "espera":
            return f"En espera: {dur}"
        return f"Transcurrido: {dur}"

    @property
    def sla_remaining_human(self) -> str:
        if not self.due_at:
            return ""
        now = datetime.datetime.utcnow()
        ref = self.closed_at if self.status == "cerrado" and self.closed_at else now
        diff = (self.due_at - ref).total_seconds()
        abs_secs = int(abs(diff))
        mins = abs_secs // 60
        hours = mins // 60
        days = hours // 24
        if days > 0:
            time_str = f"{days}d {hours % 24}h"
        elif hours > 0:
            time_str = f"{hours}h {mins % 60}m"
        else:
            time_str = f"{mins}m"

        if diff >= 0:
            return f"Restan {time_str}"
        else:
            return f"+{time_str} excedido"


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
    sla_percent = Column(Float, default=100.0)
    avg_resolution_time = Column(String(50), default="0m")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)



