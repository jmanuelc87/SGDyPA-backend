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

## Calidad de código

El repositorio incluye configuración de pre-commit con Ruff, Black y mypy.

```bash
pre-commit install
pre-commit run --all-files
```
