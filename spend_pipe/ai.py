"""Módulo AI-assisted (ver docs/ai-assist-design.md). v1: el categorizer.

Principios no negociables del diseño:
  - Determinista primero: la IA solo actúa sobre lo que las reglas YAML no
    matchearon. Nunca pisa una categoría ya asignada (por regla o por humano).
  - Sugiere, no decide: toda sugerencia lleva confidence < 1.0 y la fila se
    queda en needs_review — llega PRE-LLENADA al triage (aceptar = un click).
  - Lista CERRADA: elige de las categorías reales de Actual (cache local) o se
    abstiene. Jamás inventa categorías.
  - Opcional: sin SPENDPIPE_ANTHROPIC_KEY, is_enabled() es False y el pipeline
    sigue 100% funcional.
"""
from __future__ import annotations

import json
from decimal import Decimal

from pydantic import BaseModel

from .config import settings
from .models import AppSetting, Transaction

AI_CONFIDENCE = 0.6   # marca de origen IA: < 1.0 (determinista) y auditable
BATCH_SIZE = 40       # filas por llamada

_KEY_SETTING = "anthropic_api_key"
_MODEL_SETTING = "ai_model"


def get_api_key(session=None) -> str:
    """La key configurada desde la UI (base) gana; el env var es fallback."""
    if session is not None:
        row = session.get(AppSetting, _KEY_SETTING)
        if row is not None and row.value.strip():
            return row.value.strip()
    return settings.anthropic_key


def get_model(session=None) -> str:
    if session is not None:
        row = session.get(AppSetting, _MODEL_SETTING)
        if row is not None and row.value.strip():
            return row.value.strip()
    return settings.ai_model


def set_api_key(session, value: str) -> None:
    row = session.get(AppSetting, _KEY_SETTING)
    if row is None:
        session.add(AppSetting(key=_KEY_SETTING, value=value.strip()))
    else:
        row.value = value.strip()


def set_model(session, value: str) -> None:
    row = session.get(AppSetting, _MODEL_SETTING)
    if row is None:
        session.add(AppSetting(key=_MODEL_SETTING, value=value.strip()))
    else:
        row.value = value.strip()


def mask_key(key: str) -> str:
    """'sk-ant-api03-abc...xyz' → 'sk-ant-…h4tz' para mostrar sin exponer."""
    if not key:
        return ""
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "•••"


def is_enabled(session=None) -> bool:
    return bool(get_api_key(session))


# ── Contrato de salida (structured outputs valida contra esto) ───────────────
class Suggestion(BaseModel):
    txn_id: str
    category: str | None   # una de la lista cerrada, o null si no está claro
    reason: str             # una línea, para auditoría en logs


class SuggestionBatch(BaseModel):
    suggestions: list[Suggestion]


_SYSTEM = """Eres el categorizador de un pipeline de finanzas personales (España y México).
Recibes movimientos bancarios que las reglas deterministas no pudieron categorizar,
y una lista CERRADA de categorías válidas.

Reglas estrictas:
- Elige la categoría EXACTA de la lista (cópiala literal, formato 'Grupo / Nombre').
- Si no estás razonablemente seguro, devuelve null — abstenerse es mejor que adivinar.
- Los payees pueden venir truncados (~14 chars) o con prefijos de wallet ('Apple pay:').
- Montos negativos son gastos; positivos son ingresos/devoluciones.
- Devuelve una sugerencia por cada movimiento recibido, con su txn_id intacto."""


def _payload(txns: list[Transaction]) -> str:
    rows = [
        {
            "txn_id": t.id,
            "payee": t.raw_payee,
            "amount": str(t.amount),
            "date": t.date.isoformat(),
            "account": t.source_account,
            "currency": t.currency,
        }
        for t in txns
    ]
    return json.dumps(rows, ensure_ascii=False)


def suggest_categories(
    txns: list[Transaction],
    categories: list[str],
    client=None,
    session=None,
) -> dict[str, str]:
    """Devuelve {txn_id: category} para las filas donde la IA sugirió algo válido.

    `client` inyectable para tests; en producción se crea con la key configurada
    (UI/base primero, env var como fallback).
    """
    if not txns or not categories:
        return {}
    if client is None:
        key = get_api_key(session)
        if not key:
            return {}
        import anthropic
        client = anthropic.Anthropic(api_key=key)

    valid = set(categories)
    out: dict[str, str] = {}
    catalog = "\n".join(f"- {c}" for c in sorted(valid))

    for i in range(0, len(txns), BATCH_SIZE):
        chunk = txns[i:i + BATCH_SIZE]
        response = client.messages.parse(
            model=get_model(session),
            max_tokens=4096,
            system=f"{_SYSTEM}\n\nCategorías válidas:\n{catalog}",
            messages=[{"role": "user", "content": _payload(chunk)}],
            output_format=SuggestionBatch,
        )
        batch: SuggestionBatch = response.parsed_output
        ids = {t.id for t in chunk}
        for s in batch.suggestions:
            # Candado: id conocido y categoría de la lista cerrada, o se descarta.
            if s.txn_id in ids and s.category in valid:
                out[s.txn_id] = s.category
    return out


def apply_suggestions(txns: list[Transaction], suggestions: dict[str, str]) -> int:
    """Pre-llena category+confidence en filas SIN categoría. No cambia el status:
    needs_review se mantiene — el humano confirma en el triage."""
    applied = 0
    for t in txns:
        if t.category is None and t.id in suggestions:
            t.category = suggestions[t.id]
            t.confidence = AI_CONFIDENCE
            applied += 1
    return applied
