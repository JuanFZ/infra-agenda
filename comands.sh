

La base de datos central de tu sistema es una base de datos relacional **SQLite** que corre en el backend y se almacena en el archivo **`dashboard.db`** (dentro del volumen persistente de Docker `dashboard_data`).

En ella se consolida toda la información unificada del sistema en las siguientes tablas principales:

| Tabla | Contenido |
| :--- | :--- |
| **`items`** | Todas las actividades y tareas consolidadas (origen, estado Kanban, tiempos, responsable, cliente, notas, etc.). |
| **`users`** | Usuarios, roles (`coordinador`, `especialista`, `analista`), contraseñas encriptadas, fotos y perfiles. |
| **`escalation_documents`** | Repositorio de escalamientos a Google Docs, categorías y recomendaciones técnicas. |
| **`ibps`** | Catálogo e inventario de instituciones bancarias SIMF / IBPs. |
| **`generated_reports`** | Historial de reportes ejecutivos generados. |
| **`sync_logs`** | Bitácora y auditoría de sincronizaciones periódicas. |
| **`alert_logs`** | Registro histórico de alertas enviadas a Telegram. |

---

### Opciones para consultar la base de datos completa

#### Opción 1: Consulta directa desde la terminal del servidor (Docker CLI)
Puedes entrar directamente a consultar la base de datos con el cliente de SQLite dentro del contenedor:

```bash
# 1. Ver el listado de todas las tablas existentes
docker exec -it dashboard_backend sqlite3 /app/dashboard.db ".tables"

# 2. Ver la estructura (esquema) de la tabla de actividades
docker exec -it dashboard_backend sqlite3 /app/dashboard.db ".schema items"

# 3. Consultar las actividades con formato de columnas legible
docker exec -it dashboard_backend sqlite3 -header -column /app/dashboard.db \
  "SELECT id, source, external_id, title, status, assignee_name, created_at FROM items ORDER BY id DESC LIMIT 10;"

# 4. Abrir la consola interactiva de SQLite
docker exec -it dashboard_backend sqlite3 /app/dashboard.db
```
*(Para salir de la consola interactiva escribe `.quit` y presiona Enter).*

---

#### Opción 2: Abrirla en una herramienta gráfica (DB Browser for SQLite / DBeaver) — *Recomendado*
Si prefieres ver todas las tablas en una interfaz visual tipo Excel o Access:

1. **Descarga el archivo `.db` desde el servidor**:
   En el servidor ejecuta:
   ```bash
   docker cp dashboard_backend:/app/dashboard.db ./dashboard.db
   ```
2. Descárgalo a tu equipo (mediante SCP, WinSCP o FileZilla).
3. Ábrelo con **[DB Browser for SQLite](https://sqlitebrowser.org/)** o **DBeaver**:
   - Podrás ver todas las filas, filtrar visualmente, exportar a CSV/Excel y ejecutar consultas SQL libres.

---

#### Opción 3: Explorador Web de la API (Swagger UI)
El backend incluye una consola interactiva web donde puedes consultar todos los datos en vivo en formato JSON:

- Abre en tu navegador:
  ```
  http://<IP_DE_TU_SERVIDOR>:8000/docs
  ```
- Desde allí puedes consultar los endpoints como `GET /api/items`, `GET /api/users`, `GET /api/escalations`, etc., con sus filtros en tiempo real.