# Propuesta Estratégica: Evolución de la Agenda de Infra y BD

**Destinatario:** Coordinación de Infraestructura y Base de Datos (Sycom)  
**Autor:** Equipo de Infraestructura y Desarrollo  
**Fecha:** Noviembre 2026  
**Versión:** 2.9.2 — Matriz de Implementación Completa, Logros Entregados y Estado Operativo  

---

## 1. Resumen Ejecutivo del Estado del Sistema

Se ha consolidado con éxito la **Agenda de Infra y BD**, alcanzando un sistema maduro, colaborativo y en tiempo real que integra de manera desatendida **GLPI** y **ClickUp** para todo el equipo operativo, incorporando repositorio documental, generación de informes ejecutivos, analítica visual, modo televisión y gestión de usuarios:

```mermaid
flowchart LR
    GLPI["GLPI (Tickets TI y BD)"] -->|API REST Oficial| SYNC["Sincronizador Dockerizado (Cada 1 min)"]
    CU["ClickUp (Espacio INFRABD)"] -->|API v2 Bidireccional| SYNC
    SYNC --> DB[("Base de Datos Operativa (dashboard.db)")]
    DB --> REG["Regla 24h Kanban (Histórico Permanente en Resumen)"]
    DB --> API["Backend FastAPI (Tiempos de Atención, Escalamientos, Reportes, Perfiles)"]
    API --> WEB["Agenda de Infra (Kanban, Resumen, Escalamientos, Reportes, Modo TV, Perfil)"]
    SYNC --> TG["Bot Telegram (@sycominfrabot) · Alertas Proactivas & Deep Linking"]
```

---

## 2. Matriz de Estado y Funcionalidades

| # | Módulo / Mejora | Estado Actual | Impacto Operativo |
|---|---|:---:|:---:|
| **M1** | **Tiempos de Atención y Duración Real por Estado** |  **100% OPERATIVO EN PRODUCCIÓN** | **Crítico (Resuelto)** |
| **M2** | **Bot de Alertas Proactivas y Deep Linking** (Telegram `@sycominfrabot`) |  **100% OPERATIVO EN PRODUCCIÓN** | **Crítico (Resuelto)** |
| **M3** | **Modo TV para Sala de Operaciones** (Carrusel: Kanban + Gráficos) |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M4** | **Tablero Kanban con 4 Columnas Fijas y Drag & Drop** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M5** | **Política Inteligente de 24h en Kanban vs Histórico Permanente en Resumen** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M6** | **Diseño Glassmorphism Translúcido y Cero Emojis (Iconos SVG)** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M7** | **Ajuste de Fin de Jornada en ClickUp y Horas Locales** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M8** | **Repositorio Documental de Escalamientos e Inventario IBP (SIMF)** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M9** | **Generación de Reportes Ejecutivos (Excel .xlsx y PDF Estructurado)** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M10** | **Tablero de Analítica y Gráficos Visuales de Rendimiento** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M11** | **Gestión de Perfil de Usuario, Foto/Avatar y Cambio de Contraseña** |  **100% OPERATIVO EN PRODUCCIÓN** | **Medio-Alto (Resuelto)** |
| **M12** | **Permisos Ampliados de Creación y Eliminación para Especialistas** |  **100% OPERATIVO EN PRODUCCIÓN** | **Alto (Resuelto)** |
| **M13** | **Triaje y Clasificación Asistida con Inteligencia Artificial** |  Planificado (Fase 3) | **Medio** |

---

## 3. Logros Entregados e Implementados

### 1. Tiempos de Atención y Escalado Reales
- **Desacople total de SLA rígidos:** Enfoque en la duración real y cronología operativa (duración de vida del ticket, tiempo sin atender en pendientes, tiempo activo en curso y tiempo en espera).
- **Fechas clave:** Registro explícito de fecha de recepción en la organización y fecha de escalamiento al área de Infraestructura & BD.

### 2. Política de 24 Horas en Kanban vs Histórico en Resumen
- **Flujo ágil sin saturación:** La columna *Cerrados* del Kanban únicamente muestra actividades cerradas durante las últimas 24 horas.
- **Histórico permanente:** La base de datos local y la vista de *Resumen* conservan el 100% de los tickets resueltos para auditoría, consulta y filtros históricos.

### 3. Modo TV para Salas de Operaciones y Smart TVs
- Carrusel continuo en pantalla completa con alternancia automática entre:
  1. **Tablero Kanban:** Estado en vivo de todas las tareas operativas.
  2. **Tablero de Analítica y Gráficos:** Indicadores visuales de rendimiento mensual, distribución por especialista y top clientes.
- Control interactivo: Pausar/Reanudar (`Espacio`), cambio manual (`←` / `→`) y salida rápida (`Esc`).

### 4. Creación y Eliminación de Actividades por Especialistas
- Los especialistas pueden crear nuevas actividades operativas con sincronización bidireccional inmediata hacia ClickUp.
- Los especialistas tienen la potestad de eliminar las actividades creadas por ellos mismos, mientras que la Coordinación mantiene facultades de eliminación global.

### 5. Repositorio Documental de Escalamientos y Catálogo IBP
- Repositorio centralizado de enlaces directos a Google Docs de requerimientos escalados.
- Clasificación por categorías técnicas (CPU, RAM, Disco, Antivirus, PostgreSQL, Servicios, Infraestructura) y vinculación al catálogo de instituciones bancarias SIMF / IBPs.

### 6. Reportes Ejecutivos (Excel y PDF) y Analítica
- Generación con un solo clic de informes ejecutivos en formato **Excel (.xlsx)** con fórmulas y **PDF estructurado** con resumen para Gerencia.
- Visualización gráfica de volumen de tickets, cumplimiento y distribución por especialista.

### 7. Perfil de Usuario y Seguridad
- Módulo **Mi Perfil** accesible desde el menú lateral y la cabecera:
  - Personalización de foto de perfil (avatar recortado y optimizado en base64).
  - Actualización de datos de contacto (correo, teléfono, usuario de Telegram, cargo y especialidad).
  - Cambio seguro de contraseñas con verificación de clave actual.
- Interfaz de acceso limpia sin usuarios ni contraseñas guardadas por defecto.

### 8. Alertas Proactivas en Telegram (@sycominfrabot)
- Notificaciones automáticas ante nuevos casos y asignaciones.
- Enlaces directos (*deep linking*) que abren y enfocan automáticamente la actividad en el navegador al hacer clic en el mensaje de Telegram.

---

## 4. Próxima Fase Planificada (Fase 3)

### M13. Triaje y Clasificación Asistida con Inteligencia Artificial
- Sugerencia automática de técnico responsable y cliente basada en el asunto y descripción del ticket.
- Detección de patrones de incidentes recurrentes en bases de datos e infraestructura para generación de alertas preventivas.
