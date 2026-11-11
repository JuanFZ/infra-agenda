"""Script de migración automatizada e idempotente de SQLite a PostgreSQL para Tasks-Infra.

Transfiere íntegramente las tablas:
- users
- ibps
- escalation_documents
- items
- generated_reports
- alert_logs
- sync_logs

Preserva los IDs primarios originales y actualiza las secuencias de PostgreSQL
(SELECT setval) para evitar colisiones en futuros INSERTs automáticos.
"""

import os
import sys
import argparse
import datetime
from pathlib import Path
from sqlalchemy import create_engine, MetaData, Table, select, text, inspect

TABLE_ORDER = [
    "users",
    "ibps",
    "escalation_documents",
    "items",
    "generated_reports",
    "alert_logs",
    "sync_logs",
]

def find_sqlite_db(custom_path: str | None = None) -> str | None:
    candidates = []
    if custom_path:
        candidates.append(Path(custom_path))
    candidates.extend([
        Path("/app/dashboard.db"),
        Path("/app/data/dashboard.db"),
        Path("dashboard.db"),
        Path("backend/dashboard.db"),
        Path("../dashboard.db"),
        Path(__file__).resolve().parent / "dashboard.db",
        Path(__file__).resolve().parent.parent / "dashboard.db",
    ])
    for c in candidates:
        if c.is_file() and c.stat().st_size > 0:
            return str(c.resolve())
    return None

def migrate(sqlite_path: str, pg_url: str, force: bool = False) -> bool:
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Iniciando proceso de migración...")
    print(f" -> Origen SQLite: {sqlite_path}")
    masked_url = pg_url.split("@")[-1] if "@" in pg_url else "PostgreSQL"
    print(f" -> Destino PostgreSQL: ...@{masked_url}")

    sqlite_engine = create_engine(f"sqlite:///{sqlite_path}")
    pg_engine = create_engine(pg_url)

    # 1. Validar conexión PostgreSQL
    try:
        with pg_engine.connect() as pg_conn:
            pg_conn.execute(text("SELECT 1"))
    except Exception as e:
        print(f"[ERROR] No se pudo conectar a PostgreSQL: {e}")
        return False

    # 2. Asegurar que las tablas existan en PostgreSQL
    from app.models import Base
    Base.metadata.create_all(bind=pg_engine)

    # 3. Validar si ya existen datos en PostgreSQL
    pg_inspector = inspect(pg_engine)
    pg_tables = pg_inspector.get_table_names()

    if not force and "items" in pg_tables:
        with pg_engine.connect() as pg_conn:
            item_count = pg_conn.execute(text("SELECT COUNT(*) FROM items")).scalar() or 0
            if item_count > 0:
                print(f"[INFO] PostgreSQL ya contiene {item_count} actividades en 'items'. Migración omitida (usa --force para sobreescribir).")
                return True

    sqlite_meta = MetaData()
    sqlite_meta.reflect(bind=sqlite_engine)

    pg_meta = MetaData()
    pg_meta.reflect(bind=pg_engine)

    total_migrated = 0

    with pg_engine.begin() as pg_conn:
        if force:
            print("[INFO] Modo --force activo: truncando tablas de destino en orden inverso...")
            for table_name in reversed(TABLE_ORDER):
                if table_name in pg_tables:
                    try:
                        pg_conn.execute(text(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE"))
                    except Exception as tr_err:
                        pg_conn.execute(text(f"DELETE FROM {table_name}"))

        with sqlite_engine.connect() as sqlite_conn:
            for table_name in TABLE_ORDER:
                if table_name not in sqlite_meta.tables:
                    continue
                if table_name not in pg_meta.tables:
                    continue

                sqlite_table = sqlite_meta.tables[table_name]
                pg_table = pg_meta.tables[table_name]

                # Leer registros de SQLite
                rows = sqlite_conn.execute(select(sqlite_table)).fetchall()
                if not rows:
                    print(f" -> Tabla '{table_name}': 0 registros en SQLite, omitiendo.")
                    continue

                # Mapear columnas compatibles
                pg_columns = {c.name for c in pg_table.columns}
                row_dicts = []
                for row in rows:
                    mapping = dict(row._mapping)
                    filtered = {k: v for k, v in mapping.items() if k in pg_columns}
                    row_dicts.append(filtered)

                if row_dicts:
                    # Insertar en PostgreSQL preservando IDs
                    if not force:
                        # Si no es force, omitir duplicados por ID
                        existing_ids = set()
                        try:
                            res = pg_conn.execute(text(f"SELECT id FROM {table_name}"))
                            existing_ids = {r[0] for r in res.fetchall()}
                        except Exception:
                            pass
                        to_insert = [d for d in row_dicts if d.get("id") not in existing_ids]
                    else:
                        to_insert = row_dicts

                    if to_insert:
                        pg_conn.execute(pg_table.insert(), to_insert)
                        print(f" [OK] Tabla '{table_name}': {len(to_insert)} registros migrados con éxito.")
                        total_migrated += len(to_insert)
                    else:
                        print(f" [INFO] Tabla '{table_name}': todos los registros ya existían en PostgreSQL.")

                # Sincronizar secuencia en PostgreSQL para auto-increment
                try:
                    seq_sync_sql = text(f"""
                        SELECT setval(
                            pg_get_serial_sequence('{table_name}', 'id'),
                            COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                            (SELECT MAX(id) IS NOT NULL FROM {table_name})
                        )
                    """)
                    pg_conn.execute(seq_sync_sql)
                    print(f"      Secuencia id sincronizada para '{table_name}'.")
                except Exception as seq_err:
                    # En tablas que no usen secuencia estándar, no es error crítico
                    pass

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Migración completada exitosamente. Total registros insertados: {total_migrated}")
    return True

def auto_migrate_if_needed():
    """Ejecutado al iniciar el backend: si DATABASE_URL es PostgreSQL y la BD está vacía, migra automáticamente de dashboard.db si existe."""
    pg_url = os.getenv("DATABASE_URL", "")
    if not (pg_url.startswith("postgresql://") or pg_url.startswith("postgres://")):
        return

    sqlite_file = find_sqlite_db()
    if not sqlite_file:
        return

    try:
        pg_engine = create_engine(pg_url)
        inspector = inspect(pg_engine)
        tables = inspector.get_table_names()
        if "items" in tables:
            with pg_engine.connect() as conn:
                count = conn.execute(text("SELECT COUNT(*) FROM items")).scalar() or 0
                if count > 0:
                    return
        print(f"[Auto-Migrate] Base de datos PostgreSQL vacía detectada. Migrando automáticamente desde '{sqlite_file}'...")
        migrate(sqlite_file, pg_url, force=False)
    except Exception as e:
        print(f"[Auto-Migrate Warning] No se pudo completar la auto-migración: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrador de SQLite a PostgreSQL para Tasks-Infra")
    parser.add_argument("--sqlite-path", help="Ruta al archivo SQLite .db de origen", default=None)
    parser.add_argument("--pg-url", help="Cadena de conexión PostgreSQL DATABASE_URL", default=None)
    parser.add_argument("--force", action="store_true", help="Sobrescribir tablas de destino si ya existen datos")
    args = parser.parse_args()

    sqlite_db = find_sqlite_db(args.sqlite_path)
    if not sqlite_db:
        print("[ERROR] No se encontró ningún archivo SQLite 'dashboard.db' con datos.")
        sys.exit(1)

    pg_conn_str = args.pg_url or os.getenv("DATABASE_URL")
    if not pg_conn_str or pg_conn_str.startswith("sqlite"):
        print("[ERROR] DATABASE_URL no apunta a una base de datos PostgreSQL válida.")
        print("Ejemplo: postgresql://tasks_user:tasks_secret@localhost:5432/tasks_db")
        sys.exit(1)

    ok = migrate(sqlite_db, pg_conn_str, force=args.force)
    sys.exit(0 if ok else 1)
