FROM node:20-bookworm-slim AS node

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SPENDPIPE_STORAGE_PATH=/data \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local /usr/local

COPY pyproject.toml README.md alembic.ini ./
COPY spend_pipe ./spend_pipe
COPY alembic ./alembic
COPY rules ./rules
COPY node-pusher ./node-pusher

RUN pip install .
RUN cd node-pusher && npm ci --omit=dev
RUN mkdir -p /data

EXPOSE 8000

CMD alembic upgrade head && uvicorn spend_pipe.web.app:app --host 0.0.0.0 --port $PORT
