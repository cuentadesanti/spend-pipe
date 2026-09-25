"""Servicio mínimo con solo /api/parse: sin base de datos ni migraciones.

    uvicorn spend_pipe.parse_app:app --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI

from .api_parse import router

app = FastAPI(title="spend-pipe parse")
app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
