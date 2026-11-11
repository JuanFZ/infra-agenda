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
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar dependencias e instalarlas sin cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código de la aplicación y scripts auxiliares
COPY app/ ./app/
COPY migrate_sqlite_to_pg.py .

# Exponer el puerto de la API FastAPI
EXPOSE 8000

# Comando de inicio del servidor con uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

