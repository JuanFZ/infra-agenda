# Imagen base oficial y ligera de Python 3.11
FROM python:3.11-slim

# Evitar que Python escriba archivos .pyc y activar logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Instalar utilidades del sistema (sqlite3, postgresql-client y librerías de conexión para PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    postgresql-client \
    libpq-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Usuario no privilegiado fijo para ejecutar uvicorn en producción (Fase 3A)
RUN groupadd --gid 10001 --system appuser \
    && useradd --uid 10001 --system --gid appuser --home-dir /app --no-create-home --shell /usr/sbin/nologin appuser

# Copiar dependencias e instalarlas sin cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la aplicación con propietario no-root y preparar almacenamiento persistente
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser migrate_sqlite_to_pg.py .
RUN mkdir -p /app/data/uploads/escalations && chown -R appuser:appuser /app

USER appuser:appuser

# Exponer el puerto de la API FastAPI
EXPOSE 8000

# Comando de inicio del servidor con uvicorn bajo usuario no-root
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
