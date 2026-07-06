# SGDyPA-backend

Backend para el Software de Gestión Documental y Procesos de Auditoría.

## Arquitectura

SGDyPA se organiza como un monolito modular Django: un solo servicio de aplicación y varias apps internas que representan bounded contexts. Esta decisión sigue la recomendación de máxima sencillez sobre PostgreSQL y un único servicio de aplicación documentada en `docs/SGDyPA-docs/design-doc-capa-auditoria-sgd.md` y la organización interna definida en `docs/SGDyPA-docs/stack-tecnologico-sgd.md`.

Regla de arquitectura: **los límites son axes of change, no red**. Es decir, los módulos separan razones de cambio, propiedad del dominio y vocabulario; no son microservicios ni fronteras de despliegue. Distribuirlos en servicios separados solo se consideraría si la escala lo exigiera.

## Mapa de módulos

| App Django | Bounded context | Responsabilidad inicial |
| --- | --- | --- |
| `identity` | Identidad y autorización | Usuarios locales, organizaciones, membresías y resolución del `sub` de Keycloak. |
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
| Keycloak 26.x | `http://localhost:8080` (`admin` / `admin`) | Realm `sgdypa`, cliente público `sgdypa-spa` con PKCE (mapper de audiencia que emite `sgdypa-api`), cliente bearer-only `sgdypa-api` y usuario `dev-admin` / `dev-admin`. |
| MinIO Object Lock | API `http://localhost:9000`, consola `http://localhost:9001` (`minioadmin` / `minioadmin`) | Bucket `sgdypa-documents` creado con Object Lock, versioning y retención compliance de 30 días. |
| Redis | `localhost:6379` | AOF habilitado para desarrollo. |
| Apache Tika | `http://localhost:9998` | Imagen full con OCR/Tesseract disponible para el pipeline de extracción. |

Las variables de puertos y credenciales se pueden sobrescribir con variables de entorno (`POSTGRES_PORT`, `KEYCLOAK_PORT`, `MINIO_*`, `REDIS_PORT`, `TIKA_PORT`) antes de ejecutar Compose. Los datos persistentes viven en volúmenes Docker nombrados; para reiniciar desde cero usa `docker compose down -v`.

## Ejecutar la aplicación

La aplicación Django corre en el host; los servicios de soporte (PostgreSQL, Keycloak, MinIO, Redis, Tika, Celery) corren en Docker Compose. El proyecto usa [`uv`](https://docs.astral.sh/uv/) con Python 3.12 (fijado en `.python-version`) y por defecto carga `config.settings.dev`.

1. **Levantar los servicios de soporte** y esperar a que estén sanos:

   ```bash
   docker compose up -d
   docker compose ps
   ```

2. **Instalar dependencias** desde `uv.lock`:

   ```bash
   uv sync
   ```

3. **Configurar variables de entorno**. Copia la plantilla y ajusta los valores; `config.settings.base` carga `.env` automáticamente al arrancar (las variables reales del entorno tienen prioridad sobre el archivo). El archivo `.env` está en `.gitignore`.

   ```bash
   cp .env.example .env
   ```

4. **Aplicar migraciones** (crea el schema; ver el bootstrap de RLS por tenant más arriba):

   ```bash
   uv run python manage.py migrate
   ```

5. **Arrancar el servidor de desarrollo**:

   ```bash
   uv run python manage.py runserver
   ```

   La API queda disponible en `http://localhost:8000/api/v1/`.

6. **Verificar** con el endpoint de salud, que es el único público (`security: []` en el contrato):

   ```bash
   curl http://localhost:8000/api/v1/health-checks
   ```

Para otro entorno, fija `DJANGO_SETTINGS_MODULE` explícitamente (por ejemplo `DJANGO_SETTINGS_MODULE=config.settings.stage`). El resto de endpoints requiere un bearer token de Keycloak y, para recursos de dominio, el header `X-Organization-Id`.

## Jobs asíncronos

Celery usa Redis como broker y result backend por defecto (`redis://localhost:6379/0`). La app Celery es `config` (ver `config/celery.py`), así que el worker se arranca con `-A config`. **El worker debe estar corriendo para que se procesen las tareas encoladas** (por ejemplo la réplica de usuarios de Keycloak): un endpoint puede responder `202` y encolar la tarea, pero sin worker esa tarea se queda en la cola de Redis y nunca se aplica.

Redis debe estar arriba antes de arrancar el worker:

```bash
docker compose up -d redis
```

### Opción A — worker en el host (recomendada para desarrollo)

Corre el worker en el host con `uv`; así `localhost` resuelve tanto a Postgres (`localhost:5432`, puerto publicado por Compose) como a Redis (`localhost:6379`), igual que el `runserver` del host:

```bash
# Worker (procesa las tareas encoladas):
uv run celery -A config worker --loglevel=INFO

# Beat (solo si necesitas tareas programadas; la réplica por webhook no lo requiere):
uv run celery -A config beat --loglevel=INFO
```

Para detenerlo: `Ctrl-C`, o si quedó en segundo plano, `pkill -f "celery -A config worker"`.

### Opción B — worker en Docker Compose

Compose define los servicios `celery-worker` y `celery-beat`:

```bash
docker compose up -d redis celery-worker celery-beat
docker compose logs -f celery-worker   # ver cómo consume las tareas
```

> **Cuidado con la base de datos en Docker.** Los servicios `celery-worker`/`celery-beat` montan el repo (incluido `.env`) y no fijan `POSTGRES_HOST`. Si tu `.env` tiene `DB_ENGINE=postgres` con `POSTGRES_HOST=localhost`, dentro del contenedor `localhost` es el propio contenedor, **no** el servicio `postgres` de Compose, y el worker no podrá alcanzar la base que usa el `runserver` del host. Para usar el worker en Docker contra el Postgres de Compose, fija `POSTGRES_HOST=postgres` (y `CELERY_BROKER_URL=redis://redis:6379/0`) en el entorno del contenedor. En desarrollo, la Opción A evita este problema.

Por defecto el worker carga `config.settings.dev` (definido en `config/celery.py`). Para otro entorno, exporta `DJANGO_SETTINGS_MODULE` antes del comando.

Convención de tareas: toda tarea Celery de SGDyPA debe aceptar un `idempotency_key` estable del recurso/operación que la dispara. Las operaciones diferidas expuestas por API deben devolver `202 Accepted` y permitir sondeo por `GET` del recurso creado; el recurso base disponible es `POST /api/v1/platform/async-jobs` con header `Idempotency-Key`, seguido de `GET /api/v1/platform/async-jobs/{id}`.

### Autenticación bearer OIDC

El backend valida los access tokens de Keycloak con estas variables de entorno (definidas en `.env`; ver el paso de configuración en «Ejecutar la aplicación»):

| Variable | Valor de desarrollo | Descripción |
| --- | --- | --- |
| `KEYCLOAK_OIDC_ISSUER` | `http://localhost:8080/realms/sgdypa` | Emisor (`iss`) esperado del realm. |
| `KEYCLOAK_OIDC_AUDIENCE` | `sgdypa-api` | Audiencia (`aud`) exigida. El cliente `sgdypa-spa` la emite con el mapper `sgdypa-api-audience`. |
| `KEYCLOAK_OIDC_JWKS_URL` | `http://localhost:8080/realms/sgdypa/protocol/openid-connect/certs` | Endpoint JWKS para verificar la firma RS256. |
| `KEYCLOAK_OIDC_ALGORITHMS` | `RS256` | Algoritmos de firma aceptados (lista separada por comas). |

El realm importado ya alinea el token emitido con esta validación: el cliente público `sgdypa-spa` incluye un mapper de audiencia que añade `sgdypa-api` al access token, y `sgdypa-api` existe como cliente bearer-only que representa al recurso.

### Proyección de usuario local (provisioning)

La autenticación es *fail-closed*: el backend nunca crea usuarios a partir de un token. Cada token válido debe corresponder a un `User` local previamente proyectado, anclado por el `sub` de Keycloak (`keycloak_sub`). Si no existe, la API responde `401` con `{"detail": "No local user projection exists for the token subject."}`.

Usa el comando de management `provision_user` (solo para desarrollo/pruebas) para crear o actualizar esa proyección. La forma más simple es pasarle el propio access token; se decodifica **sin verificar la firma**, únicamente para leer los claims `sub`, `email` y `name`:

```bash
uv run python manage.py provision_user --token "<access-token>"
```

Esto es suficiente para `GET /api/v1/me`. Para endpoints con alcance de organización, crea además la organización, la membresía activa y un rol del sistema (`P1`–`P7`); el comando imprime el valor de `X-Organization-Id` que debes enviar en esas peticiones:

```bash
uv run python manage.py provision_user --token "<access-token>" \
  --org-slug default-org --org-name "Default Org" --role P6
```

Alternativamente puedes pasar `--sub "<uuid>"` en lugar de `--token` (copiando el `sub` desde el propio token). Usa el token del mismo grant que enviarás a la API: un token de *Client Credentials* (service account) tiene un `sub` distinto al de un login de usuario.

### Replicación Keycloak → backend (admin-event webhook)

El *provisioning* manual y la sincronización en login solo refrescan un `User` que **ya existe**. Para reflejar en el backend los usuarios (y cambios de `email`, nombre, `emailVerified`, `enabled`) creados o modificados en Keycloak **sin esperar a que el usuario inicie sesión**, Keycloak envía sus *admin events* a un endpoint del backend que reconcilia la proyección.

Ruta del flujo (todo anclado en `keycloak_sub`, nunca en email — ADR-0002):

1. Keycloak dispara un *admin event* de tipo `USER` (`CREATE`/`UPDATE`/`DELETE`).
2. Un *event listener* de Keycloak hace `POST` a `POST /api/v1/identity/keycloak/events` con el payload del evento, firmado con **HMAC-SHA256** sobre el cuerpo crudo en el header `X-Keycloak-Signature` (autenticación por secreto compartido, **no** JWT).
3. El endpoint verifica la firma en tiempo constante, deduplica por `id` del evento (`identity.KeycloakReplicationEvent`), encola una tarea Celery y responde **`202`** de inmediato (*ack* rápido; el trabajo real corre en el worker).
4. La tarea `process_keycloak_admin_event` aplica la proyección vía el core compartido (`apps/identity/replication.py`): `CREATE`/`UPDATE` hacen *upsert* keyed on `sub`; `DELETE` o `enabled: false` marcan `is_active=False` (nunca se borra la fila local, protegida por membresías y el audit trail). Es idempotente: `processed_at` corta reprocesos.

El login-time sync y esta réplica comparten el mismo core, así que aplican los atributos de forma idéntica.

**Configuración del backend** (variables en `.env`; ver «Ejecutar la aplicación»):

| Variable | Valor de desarrollo | Descripción |
| --- | --- | --- |
| `KEYCLOAK_WEBHOOK_SECRET` | `dev-webhook-secret-change-me` | Secreto compartido para firmar/verificar el webhook. **Si está vacío el endpoint queda deshabilitado y responde `503` (fail-closed).** |
| `KEYCLOAK_WEBHOOK_SIGNATURE_HEADER` | `X-Keycloak-Signature` | Header con el HMAC-SHA256 (hex) del cuerpo crudo. Se acepta un prefijo opcional `sha256=`. |

**Cambios en el realm** (`docker/keycloak/sgdypa-realm.json`): se habilitan eventos de administración y se registra el listener del webhook.

```json
"eventsEnabled": true,
"adminEventsEnabled": true,
"adminEventsDetailsEnabled": true,
"eventsListeners": ["jboss-logging", "webhook"]
```

**Extensión de Keycloak (SPI):** Keycloak *no* trae un webhook HTTP integrado; el listener `webhook` lo aporta una extensión SPI que debe desplegarse dentro de la imagen de Keycloak. El backend es agnóstico a la extensión concreta siempre que respete el contrato: `POST` del *admin event* con el header de firma HMAC-SHA256. Para desarrollo se recomienda una extensión de webhook que lea su configuración por entorno; hornea el JAR en la imagen y apúntala al backend con el mismo secreto:

```dockerfile
# docker/keycloak/Dockerfile (ejemplo)
FROM quay.io/keycloak/keycloak:26.6.0
# Copia el JAR del SPI de webhook a /opt/keycloak/providers/
COPY providers/keycloak-webhook.jar /opt/keycloak/providers/
RUN /opt/keycloak/bin/kc.sh build
```

```yaml
# variables de entorno del contenedor keycloak (según la extensión elegida)
WEBHOOK_HTTP_BASE_PATH: http://host.docker.internal:8000/api/v1/identity/keycloak/events
WEBHOOK_HTTP_AUTH_HMAC_SECRET: ${KEYCLOAK_WEBHOOK_SECRET:-dev-webhook-secret-change-me}
```

**Contrato del payload esperado** (forma del *admin event* de Keycloak; `representation` llega como string JSON):

```json
{
  "id": "0b8f...-evento",
  "type": "admin.USER-CREATE",
  "operationType": "CREATE",
  "resourceType": "USER",
  "resourcePath": "users/8f3c...-uuid-del-usuario",
  "representation": "{\"id\":\"8f3c...\",\"email\":\"user@example.com\",\"firstName\":\"...\",\"lastName\":\"...\",\"enabled\":true,\"emailVerified\":true}"
}
```

El `sub` (id del usuario de Keycloak) se toma de `representation.id` o, en su defecto, del segmento `users/{id}` de `resourcePath`. Los eventos que no son de recurso `USER` se aceptan con `202 {"status":"ignored"}` para que Keycloak no los reintente.

> **El worker de Celery debe estar corriendo.** El endpoint responde `202` y encola la tarea, pero la escritura en `identity_user` la aplica `process_keycloak_admin_event` en el worker. Sin worker, verás `202` pero **no** habrá sync. Arráncalo según «[Jobs asíncronos](#jobs-asíncronos)».

Prueba manual del endpoint. La firma HMAC se calcula sobre los **bytes exactos** del cuerpo, así que hay que firmar y enviar los mismos bytes. Lo más robusto es escribir el cuerpo a un archivo (con `printf`, **sin** salto de línea final) y enviarlo con `--data-binary` (que no altera el contenido):

```bash
SECRET='dev-webhook-secret-change-me'

# Cuerpo a un archivo con printf (NO uses echo: añade un \n y cambia el hash).
printf '%s' '{"id":"evt-1","operationType":"CREATE","resourceType":"USER","resourcePath":"users/kc-sub-1","representation":"{\"id\":\"kc-sub-1\",\"email\":\"user@example.com\",\"enabled\":true}"}' > /tmp/kc-event.json

# Firma ese archivo exacto.
SIG=$(openssl dgst -sha256 -hmac "$SECRET" /tmp/kc-event.json | awk '{print $NF}')

# Envía ese archivo exacto con --data-binary (no --data, que puede alterar bytes).
curl -sS -X POST http://localhost:8000/api/v1/identity/keycloak/events \
  -H "Content-Type: application/json" \
  -H "X-Keycloak-Signature: $SIG" \
  --data-binary @/tmp/kc-event.json
# -> 202 {"status": "accepted", "event_id": "evt-1"}
```

> Si obtienes `401 {"code": "authentication_failed", ...}`, casi siempre es que los bytes firmados no coinciden con los enviados: firmaste con `echo` (añade `\n`), enviaste con `--data` (puede quitar/alterar bytes), o el `KEYCLOAK_WEBHOOK_SECRET` real del entorno difiere del usado al firmar (una variable de entorno exportada tiene prioridad sobre `.env`).

> **Nota sobre el audit trail:** `trail.TrailEntry` es *append-only* pero está acotado a `organization` + `actor` (ambos obligatorios). Un evento de réplica es global e iniciado por el sistema (sin organización ni usuario actor), por lo que no encaja en el ledger actual; estas escrituras se registran vía *structured logging* (`apps.identity.replication`). Si se requiere auditoría formal de la réplica, haría falta un trail con alcance de sistema.

### Configuración del cliente frontend (SPA)

El realm seed ya deja provisionado el cliente que consume la SPA. **La SPA vive en un repositorio aparte**; aquí solo se configura Keycloak para que ese frontend pueda autenticarse. El cliente debe usar estos valores (definidos en `docker/keycloak/sgdypa-realm.json`):

| Parámetro | Valor de desarrollo | Descripción |
| --- | --- | --- |
| Client ID | `sgdypa-spa` | Cliente público (sin secreto) para el navegador. |
| Flujo | Authorization Code + PKCE (`S256`) | `standardFlowEnabled`; método de challenge `pkce.code.challenge.method=S256`. |
| Issuer | `http://localhost:8080/realms/sgdypa` | Emisor del realm; base para los endpoints OIDC. |
| Redirect URIs | `http://localhost:5173/*`, `http://127.0.0.1:5173/*` | Puerto por defecto de Vite. |
| Web Origins (CORS) | `http://localhost:5173`, `http://127.0.0.1:5173` | Orígenes permitidos para el intercambio de tokens. |
| Audiencia emitida | `sgdypa-api` | El mapper `sgdypa-api-audience` añade `sgdypa-api` al `aud` del access token, que es lo que exige el backend. |
| Usuario de prueba | `dev-admin` / `dev-admin` (email `dev-admin@sgdypa.local`) | Usuario seed del realm para login local; sembrado en `docker/keycloak/sgdypa-realm.json` con `emailVerified: true` y contraseña no temporal, por lo que la SPA puede iniciar sesión directamente sin cambio de contraseña. Acepta como identificador tanto el username como el email. |

Flujo de extremo a extremo: la SPA inicia sesión contra Keycloak con Authorization Code + PKCE, obtiene un access token con `aud = sgdypa-api` y llama al backend en `http://localhost:8000/api/v1/` con `Authorization: Bearer <token>`. Solo `GET /api/v1/health-checks` es público; el resto de endpoints requiere el token y, para recursos de dominio, el header `X-Organization-Id`.

Notas operativas:

- Si la SPA no corre en el puerto `5173`, agrega su URL a `redirectUris` y `webOrigins` del cliente `sgdypa-spa` en `docker/keycloak/sgdypa-realm.json`; de lo contrario Keycloak rechaza el login con `Invalid redirect_uri`.
- El realm se importa en el primer arranque de Keycloak. Tras editar el JSON del realm, vuelve a importarlo o reinicia desde cero con `docker compose down -v` para re-sembrar.
