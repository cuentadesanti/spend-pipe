"""Exporta un batch aprobado desde staging (filas planas) al artefacto agrupado por
cuenta que consume el worker Node. Esta es la frontera Python → Node.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from datetime import datetime, timezone

from .config import settings
from .models import Batch, Transaction
from .schema import (
    BatchAccountGroup,
    BatchArtifact,
    BatchTransaction,
    BatchTransactionMeta,
)

# Mapeo source_account (staging) → nombre de cuenta en Actual. Por defecto identidad:
# el source_account ya es el nombre en Actual (ej. 'BBVA TDC'). Excepciones explícitas acá.
#
# Openbank tarjeta (source_account 'Openbank Tarjeta'): la cuenta destino depende del tipo
# de tarjeta — decisión explícita, NO cambiar el source_account (rompería imported_id):
#   - CRÉDITO → cuenta separada en Actual; el pago mensual se concilia como transferencia.
#   - DÉBITO  → carga directa contra 'Openbank Nómina' (cuenta ...8579), sin cuenta aparte.
# Openbank es DÉBITO: las compras cargan directo contra 'Openbank Nómina' (...8579).
# NO importar además el extracto de Nómina o se duplican estos mismos cargos.
# El source_account 'Openbank Tarjeta' se mantiene estable (es parte del imported_id);
# solo cambia el ruteo hacia Actual.
# OJO: en Actual las cuentas EUR/MXN llevan el sufijo de moneda en el nombre
# (ej. "Openbank Nómina (EUR)"). El nombre debe coincidir EXACTO.
ACCOUNT_MAP: dict[str, str] = {
    "Openbank Tarjeta": "Openbank Nómina (EUR)",   # débito → carga directa a Nómina (off-budget)
    "BBVA Cuenta Digital": "BBVA Cuenta Digital (MXN)",   # el nombre en Actual lleva sufijo (MXN)
}


def actual_account_name(source_account: str) -> str:
    """Cuenta destino en Actual para un source_account. También la usa el dedup:
    dos fuentes que rutean a la misma cuenta deben poder chocar entre sí."""
    return ACCOUNT_MAP.get(source_account, source_account)


_actual_account_name = actual_account_name  # alias interno


def _to_batch_txn(txn: Transaction) -> BatchTransaction:
    return BatchTransaction(
        spend_pipe_transaction_id=txn.id,
        date=txn.date,
        amount=txn.amount,
        currency=txn.currency,
        # MVP1: se envía el payee CRUDO para que las reglas ya existentes de Actual
        # (que matchean sobre imported_payee) sigan categorizando post-import.
        # El payee normalizado vive en staging solo para mostrarlo en el review.
        payee_name=txn.raw_payee,
        notes=txn.notes,
        imported_id=txn.imported_id,
        cleared=True,
        category_name=txn.category,  # null en MVP1
        metadata=BatchTransactionMeta(
            source_file=txn.source_file,
            source_row=txn.source_row,
            raw_payee=txn.raw_payee,
        ),
    )


def build_batch_artifact(
    batch: Batch,
    txns: list[Transaction],
    approved_by: str,
    currency_default: str = "MXN",
) -> BatchArtifact:
    """Agrupa por cuenta (importTransactions opera por cuenta) preservando el orden."""
    groups: "OrderedDict[str, list[Transaction]]" = OrderedDict()
    for t in txns:
        groups.setdefault(t.source_account, []).append(t)

    accounts = [
        BatchAccountGroup(
            actual_account_name=_actual_account_name(source_account),
            source_account_name=source_account,
            transactions=[_to_batch_txn(t) for t in rows],
        )
        for source_account, rows in groups.items()
    ]

    return BatchArtifact(
        batch_id=batch.id,
        approved_at=batch.approved_at or datetime.now(timezone.utc),
        approved_by=approved_by,
        currency_default=currency_default,
        accounts=accounts,
    )


def write_artifact(artifact: BatchArtifact, artifacts_dir: str | None = None) -> str:
    """Escribe batch-<id>.json y devuelve la ruta. El worker Node lo lee desde ahí."""
    out_dir = artifacts_dir or settings.artifacts_dir
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"batch-{artifact.batch_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        # model_dump con mode='json' serializa date/Decimal a string legible.
        json.dump(artifact.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    return path
