# spend-pipe

Sistema de ingesta financiera, separado del budget app. Recibe archivos mensuales de
bancos/tarjetas, los lleva a un schema común, normaliza/dedup, permite revisar y aprobar
un batch, y hace bulk-upsert idempotente contra **Actual Budget** (PikaPods).

```
inbox → ingest → parse → normalize → [dedup] → staging (SQLite/Postgres) → review web → approve
                                                                         │ batch aprobado
                                                        artifacts/batch-<id>.json
                                                                         ▼
                                          node-pusher/push.js → @actual-app/api → Actual
```

El **push es Node** (reusa `importTransactions`); todo lo de aguas arriba es **Python**.
La frontera entre ambos es el artefacto `batch-<id>.json` (`schema_version` versionado).

## Estado: MVP 1 (staging + review + push)

Hecho y con tests: parsers (CSV genérico + manual), pipeline de normalización, identidad
(`imported_id` con occurrence index + `dedup_hash`), staging, ingest idempotente, Web UI de
review/aprobación, export del artefacto y worker Node de push. Ver `MVP 2–5` en las notas
de diseño (dedup serio, categorías en-pipeline, migrar reglas de Actual, PDF BBVA robusto).

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # completá las credenciales de Actual (NO se commitean)
```

Local por defecto usa `sqlite:///./spend_pipe.db`. En producción podés pasar
`SPENDPIPE_DATABASE_URL` apuntando a PostgreSQL/Supabase y, opcionalmente,
`SPENDPIPE_STORAGE_PATH` para mover `inbox/` y `artifacts/` a un volumen persistente.

## Correr la Web UI

```bash
uvicorn spend_pipe.web.app:app --reload
# http://127.0.0.1:8000
```

La app **aplica las migraciones de Alembic a `head` al arrancar** (Alembic es la fuente
de verdad del schema). Para crear una migración nueva tras cambiar los modelos:

```bash
alembic revision --autogenerate -m "descripción"
alembic upgrade head
```

Subís un CSV (o pegás filas manuales), revisás/editás payee·categoría·status, marcás
duplicados, y aprobás → se escribe `artifacts/batch-<id>.json`.

## Deploy: Railway + Supabase

Variables de entorno mínimas:

```bash
SPENDPIPE_DATABASE_URL=postgresql://postgres:password@db.xxx.supabase.co:5432/postgres
SPENDPIPE_STORAGE_PATH=/data
SPENDPIPE_ACTUAL_SERVER_URL=...
SPENDPIPE_ACTUAL_PASSWORD=...
SPENDPIPE_ACTUAL_SYNC_ID=...
```

Notas:

- `SPENDPIPE_DATABASE_URL` acepta `postgres://...` o `postgresql://...`; la app lo
  normaliza al driver `psycopg` para SQLAlchemy/Alembic.
- Si definís `SPENDPIPE_STORAGE_PATH`, la app usa `<storage>/inbox` y
  `<storage>/artifacts` por defecto. También podés sobrescribirlos con
  `SPENDPIPE_INBOX_DIR` y `SPENDPIPE_ARTIFACTS_DIR`.
- Railway puede montar un volumen persistente en `/data`; el `Dockerfile` ya deja ese
  path como default dentro del contenedor.

Build/Run:

```bash
docker build -t spend-pipe .
docker run --rm -p 8000:8000 \
  -e SPENDPIPE_DATABASE_URL=postgresql://... \
  -e SPENDPIPE_STORAGE_PATH=/data \
  -e SPENDPIPE_ACTUAL_SERVER_URL=... \
  -e SPENDPIPE_ACTUAL_PASSWORD=... \
  -e SPENDPIPE_ACTUAL_SYNC_ID=... \
  spend-pipe
```

## Push a Actual

```bash
cd node-pusher && npm install
node push.js ../artifacts/batch-<id>.json            # dry-run: solo muestra el plan
node push.js ../artifacts/batch-<id>.json --commit   # escribe en Actual
```

El botón "Push a Actual" de la UI hace lo mismo (`--commit`) por vos.

## Tests

```bash
pytest -q
```

## Notas

- **Corte con finanzas-ai:** spend-pipe usa un formato de `imported_id` nuevo
  (`spendpipe:...`), distinto al de los scripts one-off de `finanzas-ai`. spend-pipe es
  dueño de los **meses nuevos**; no re-importar meses que finanzas-ai ya subió (duplicaría).
- **MVP 1** manda el payee **crudo** a Actual para que sus 35 reglas sigan categorizando
  post-import. Las categorías se mueven al pipeline recién en MVP 3–4.
- Secretos solo por `.env` (fuera de git). Rotar la password de PikaPods si tocó un repo.

## Servicio de parseo para ledger-guard

`POST /api/parse` recibe un extracto (multipart, campo `file`) y devuelve filas
normalizadas con `imported_id`, periodo y saldos en céntimos, sin tocar staging.
Se sirve con una app mínima sin base de datos:

    uvicorn spend_pipe.parse_app:app --host 0.0.0.0 --port 8000

Si `SPENDPIPE_PARSE_TOKEN` está definido, exige `Authorization: Bearer <token>`.
Soporta los formatos con parser registrado: BBVA (PDF de cuenta y de tarjeta, CSV
de tarjeta), Openbank (.xls de cuenta y de tarjeta) y Revolut (CSV).
