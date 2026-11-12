#!/bin/bash
# ==============================================================================
# COMANDOS ÚTILES - SYCOM AGENDA DE INFRAESTRUCTURA & BD
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. ACTUALIZAR REPOSITORIO Y LEVANTAR CONTENEDORES (Versión v3.1.3 con Auditoría)
# ------------------------------------------------------------------------------
git fetch origin --tags
git checkout v3.1.3
docker compose up -d --build

# Ver estado de los contenedores
docker compose ps

# Ver logs en vivo del backend FastAPI
docker compose logs -f dashboard_backend


# ------------------------------------------------------------------------------
# 2. CONSULTAS A LA TABLA DE AUDITORÍA (audit_logs) DIRECTO EN POSTGRESQL
# ------------------------------------------------------------------------------

# A. Ver la estructura de la nueva tabla audit_logs
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "\d audit_logs"

# B. Ver los últimos 20 eventos registrados en la bitácora
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS fecha_utc,
       username, 
       user_role, 
       action, 
       entity_type, 
       entity_id, 
       ip_address 
FROM audit_logs 
ORDER BY id DESC 
LIMIT 20;"

# C. Ver todos los inicios de sesión (Exitosos, Fallidos y Bloqueados)
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS fecha,
       username, 
       action, 
       ip_address, 
       details 
FROM audit_logs 
WHERE action IN ('LOGIN_SUCCESS', 'LOGIN_FAILED', 'LOGIN_BLOCKED') 
ORDER BY id DESC 
LIMIT 20;"

# D. Detectar intentos fallidos de inicio de sesión (posibles intrusiones / claves erróneas)
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       created_at, 
       username, 
       ip_address, 
       user_agent, 
       details 
FROM audit_logs 
WHERE action = 'LOGIN_FAILED' 
ORDER BY id DESC;"

# E. Ver cambios de estado en actividades (movimientos de tarjetas en el Tablero Kanban)
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS fecha,
       username, 
       entity_id AS item_id, 
       details 
FROM audit_logs 
WHERE action = 'UPDATE_STATUS' 
ORDER BY id DESC 
LIMIT 20;"

# F. Ver asignaciones de actividades a especialistas
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS fecha,
       username, 
       entity_id AS item_id, 
       details 
FROM audit_logs 
WHERE action = 'ASSIGN_ITEM' 
ORDER BY id DESC 
LIMIT 15;"

# G. Ver registro y modificaciones de Horas Extras
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS fecha,
       username, 
       action, 
       entity_id AS overtime_id, 
       details 
FROM audit_logs 
WHERE entity_type = 'overtime' 
ORDER BY id DESC 
LIMIT 20;"

# H. Auditoría de eliminaciones (borrado de actividades, horas extras o escalamientos)
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT id, 
       to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS fecha,
       username, 
       user_role, 
       action, 
       entity_type, 
       entity_id, 
       details 
FROM audit_logs 
WHERE action LIKE 'DELETE_%' 
ORDER BY id DESC;"

# I. Resumen estadístico: conteo total por tipo de acción
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT action, 
       entity_type, 
       COUNT(*) AS total_eventos 
FROM audit_logs 
GROUP BY action, entity_type 
ORDER BY total_eventos DESC;"

# J. Conteo de actividad por usuario
docker exec -it dashboard_db psql -U tasks_user -d tasks_db -c "
SELECT username, 
       user_role, 
       COUNT(*) AS total_acciones,
       MAX(created_at) AS ultima_accion 
FROM audit_logs 
GROUP BY username, user_role 
ORDER BY total_acciones DESC;"


# ------------------------------------------------------------------------------
# 3. CONSULTAS VÍA API REST (CURL)
# ------------------------------------------------------------------------------

# A. Ver los últimos 10 logs en formato JSON estructurado (con fecha en hora de Caracas)
curl -s "http://localhost:7070/api/audit-logs?limit=10" -H "X-User-Role: Coordinador" | python3 -m json.tool

# B. Filtrar logs de un usuario específico
curl -s "http://localhost:7070/api/audit-logs?username=j.fernandez&limit=15" -H "X-User-Role: Coordinador" | python3 -m json.tool

# C. Filtrar solo inicios de sesión
curl -s "http://localhost:7070/api/audit-logs?entity_type=auth&limit=20" -H "X-User-Role: Coordinador" | python3 -m json.tool

# D. Buscar en los detalles (por ejemplo, buscar una actividad por su título o ID)
curl -s "http://localhost:7070/api/audit-logs?search=PostgreSQL" -H "X-User-Role: Coordinador" | python3 -m json.tool
