"""Pasadas deterministas del pipeline.

Son TRES pasadas separadas a propósito (no una sola), aunque todas corran antes de
cualquier LLM. Mantenerlas separadas evita el enredo de rules.js, que renombra payee
y categoriza en la misma regla:

  1. normalize   → limpia el payee para mostrar (raw_payee queda intacto).
  2. classify    → tipo de movimiento (transfer / gasto / ingreso).  [stub en MVP1]
  3. categorize  → categoría + confidence.                            [stub en MVP1]

En MVP1 solo la (1) hace trabajo real. (2) y (3) son passthrough: no bajan la
confianza ni mandan a needs_review, porque en MVP1 el push va SIN categoría y son
las reglas ya existentes de Actual las que categorizan post-import.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..models import Transaction, TxnStatus

_WS = re.compile(r"\s+")
# Ruido común en payees crudos de tarjeta: URLs y asteriscos de agregadores de pago.
_URL = re.compile(r"\b\S+\.(?:com|net|org|mx|es)\S*", re.IGNORECASE)


def normalize_payee(raw: str | None) -> str:
    """Limpieza conservadora para mostrar. El renombrado real (UBER TRIP → Uber)
    llega con las reglas versionadas en MVP4; acá solo emprolijamos.
    """
    s = (raw or "").strip()
    s = _URL.sub("", s)          # saca 'HELP.UBER.COM' y similares
    s = s.replace("*", " ")      # 'OPENAI *CHATGPT' → 'OPENAI  CHATGPT'
    s = _WS.sub(" ", s).strip()
    if not s:
        return ""
    # Title Case suave solo si viene TODO en mayúsculas (típico de bancos).
    if s.isupper():
        s = s.title()
    return s


# ── Pasada 2: tipo de movimiento (stub) ────────────────────────────────────
def classify_type(txn: Transaction) -> bool:
    """Devuelve is_transfer. Stub en MVP1: nada es transfer todavía.
    En MVP futuro: detectar pagos de tarjeta, traspasos entre cuentas propias, etc.
    """
    return False


# ── Pasada 3: categoría (stub) ─────────────────────────────────────────────
def categorize(txn: Transaction) -> tuple[str | None, float | None]:
    """Devuelve (category_name, confidence). Stub en MVP1: sin categoría.
    En MVP3/4: reglas deterministas portadas de rules.js + LLM como sugeridor.
    """
    return (None, None)


def _needs_review(txn: Transaction) -> bool:
    """Motivos para marcar needs_review en MVP1. Deliberadamente acotado: sin reglas
    de categoría todavía, solo se resaltan filas genuinamente problemáticas.
    """
    if txn.pending:  # autorizado/no liquidado: no debe aprobarse solo
        return True
    if not (txn.payee or "").strip():
        return True
    if txn.amount is None or Decimal(txn.amount) == 0:
        return True
    return False


def run_pipeline(txn: Transaction) -> Transaction:
    """Aplica las tres pasadas sobre una fila de staging y fija su status.

    No toca `imported_id` ni `dedup_hash` (identidad; se asignan en ingest/MVP2).
    """
    txn.payee = normalize_payee(txn.raw_payee)
    txn.is_transfer = classify_type(txn)
    txn.category, txn.confidence = categorize(txn)
    txn.status = TxnStatus.needs_review if _needs_review(txn) else TxnStatus.normalized
    return txn
