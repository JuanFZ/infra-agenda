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


class SyncLog(Base):
    __tablename__ = "sync_logs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    source = Column(String(50))
    status = Column(String(50)) # 'OK', 'ERROR'
    records_synced = Column(Integer, default=0)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

