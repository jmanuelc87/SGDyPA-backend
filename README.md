# SGDyPA-backend

Backend para el Software de Gestión Documental y Procesos de Auditoría.

## Arquitectura

SGDyPA se organiza como un monolito modular Django: un solo servicio de aplicación y varias apps internas que representan bounded contexts. Esta decisión sigue la recomendación de máxima sencillez sobre PostgreSQL y un único servicio de aplicación documentada en `docs/SGDyPA-docs/design-doc-capa-auditoria-sgd.md` y la organización interna definida en `docs/SGDyPA-docs/stack-tecnologico-sgd.md`.

Regla de arquitectura: **los límites son axes of change, no red**. Es decir, los módulos separan razones de cambio, propiedad del dominio y vocabulario; no son microservicios ni fronteras de despliegue. Distribuirlos en servicios separados solo se consideraría si la escala lo exigiera.

## Mapa de módulos

| App Django | Bounded context | Responsabilidad inicial |
| --- | --- | --- |
| `identity` | Identidad y autorización | Usuarios locales, organizaciones, membresías y resolución futura de `sub` de Keycloak. |
| `documents` | Gestión documental | Documentos, versiones, metadatos, clasificación y ciclo de vida documental. |
| `retention_disposition` | Retención y disposición | Políticas de retención, solicitudes de disposición y aprobaciones. |
| `audit_process` | Proceso de auditoría ISO 19011 | Programa, estados de auditoría, transiciones y seguimiento del proceso. |
| `findings_capa` | Hallazgos y CAPA | Hallazgos, no conformidades, acciones correctivas/preventivas y eficacia. |
| `trail` | Audit trail técnico | Ledger append-only, hash chain y anclajes de evidencia. |
| `rag` | Consulta asistida | Chunking, embeddings, recuperación y respuestas con citas. |
| `platform` | Plataforma compartida | Configuración transversal, health checks y utilidades comunes. |

## Settings por entorno

El paquete `config.settings` contiene settings separados por entorno:

- `config.settings.dev` para desarrollo local.
- `config.settings.stage` para staging.
- `config.settings.prod` para producción.

Por defecto, `manage.py`, ASGI y WSGI cargan `config.settings.dev`. En despliegues se debe fijar `DJANGO_SETTINGS_MODULE` explícitamente.


## Aislamiento por tenant en PostgreSQL

El bootstrap de PostgreSQL crea el rol `sgdypa_app`, sin `BYPASSRLS`, y helpers en el schema `sgdypa` para aplicar RLS fail-closed a tablas de dominio con columna `organization_id`. Cada request debe fijar `app.current_org` con `sgdypa.set_current_organization(<org_uuid>)` como primera sentencia dentro de una transacción explícita, equivalente a `SET LOCAL`.

Para cada tabla de dominio nueva con `organization_id`, la migración que la cree debe llamar a `sgdypa.enable_organization_rls('<schema>.<tabla>'::regclass)`. Cuando exista `sgdypa.trail_entry`, debe invocar también `sgdypa.grant_trail_entry_append_only()` para conservar el ledger como append-only desde el rol de aplicación.

## Calidad de código

El repositorio incluye configuración de pre-commit con Ruff, Black y mypy.

```bash
pre-commit install
pre-commit run --all-files
```

## Entorno local con Docker Compose

El entorno de desarrollo levanta en un solo comando los servicios base del stack documentado en `docs/SGDyPA-docs/stack-tecnologico-sgd.md`: PostgreSQL 16 con pgvector, Keycloak 26.x, MinIO con Object Lock, Redis y Apache Tika.

```bash
docker compose up -d
```

Servicios expuestos por defecto:

| Servicio | URL / puerto | Seed mínimo |
| --- | --- | --- |
| PostgreSQL + pgvector | `localhost:5432` (`sgdypa` / `sgdypa_dev_password`) | Extensión `vector`, schema `sgdypa` y tabla `sgdypa.dev_seed`. |
| Keycloak 26.x | `http://localhost:8080` (`admin` / `admin`) | Realm `sgdypa`, cliente público `sgdypa-spa` con PKCE y usuario `dev-admin` / `dev-admin`. |
| MinIO Object Lock | API `http://localhost:9000`, consola `http://localhost:9001` (`minioadmin` / `minioadmin`) | Bucket `sgdypa-documents` creado con Object Lock, versioning y retención compliance de 30 días. |
| Redis | `localhost:6379` | AOF habilitado para desarrollo. |
| Apache Tika | `http://localhost:9998` | Imagen full con OCR/Tesseract disponible para el pipeline de extracción. |

Las variables de puertos y credenciales se pueden sobrescribir con variables de entorno (`POSTGRES_PORT`, `KEYCLOAK_PORT`, `MINIO_*`, `REDIS_PORT`, `TIKA_PORT`) antes de ejecutar Compose. Los datos persistentes viven en volúmenes Docker nombrados; para reiniciar desde cero usa `docker compose down -v`.

## Jobs asíncronos

Celery usa Redis como broker y result backend por defecto (`redis://localhost:6379/0`). En Compose, `celery-worker` ejecuta las tareas encoladas y `celery-beat` queda listo para tareas programadas.

```bash
docker compose up -d redis celery-worker celery-beat
```

Convención de tareas: toda tarea Celery de SGDyPA debe aceptar un `idempotency_key` estable del recurso/operación que la dispara. Las operaciones diferidas expuestas por API deben devolver `202 Accepted` y permitir sondeo por `GET` del recurso creado; el recurso base disponible es `POST /api/v1/platform/async-jobs` con header `Idempotency-Key`, seguido de `GET /api/v1/platform/async-jobs/{id}`.
