"""Exporta un batch aprobado desde staging (filas planas) al artefacto agrupado por
cuenta que consume el worker Node. Esta es la frontera Python → Node.
"""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from datetime import datetime, timezone

from .config import settings
from .models import Batch, Transaction, TxnStatus
from .schema import (
    BatchAccountGroup,
    BatchArtifact,
    BatchSubtransaction,
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
# Tarjeta y cuenta rutean a la MISMA cuenta destino: así el dedup_hash (que usa la
# cuenta destino) hace chocar entre sí los cargos que llegan por ambas fuentes, y la
# reconciliación adopta lo que ya exista en Actual.
# Los source_account se mantienen estables (son parte del imported_id);
# solo cambia el ruteo hacia Actual.
# OJO: en Actual las cuentas EUR/MXN llevan el sufijo de moneda en el nombre
# (ej. "Openbank Nómina (EUR)"). El nombre debe coincidir EXACTO.
ACCOUNT_MAP: dict[str, str] = {
    "Openbank Tarjeta": "Openbank Nómina (EUR)",   # débito → carga directa a Nómina
    "Openbank Cuenta": "Openbank Nómina (EUR)",    # extracto de la misma cuenta ...8579
    "BBVA Cuenta Digital": "BBVA Cuenta Digital (MXN)",   # el nombre en Actual lleva sufijo (MXN)
}


def actual_account_name(source_account: str) -> str:
    """Cuenta destino en Actual para un source_account. También la usa el dedup:
    dos fuentes que rutean a la misma cuenta deben poder chocar entre sí."""
    return ACCOUNT_MAP.get(source_account, source_account)


_actual_account_name = actual_account_name  # alias interno


def _transfer_dest(txn: Transaction, session) -> str | None:
    """Cuenta destino en Actual para la pata NEGATIVA de un par de transferencia.

    Solo si el peer no está ya synced como transacción normal (en ese caso ambas
    quedan regulares: crear la contraparte duplicaría lo ya empujado)."""
    if session is None or not txn.is_transfer or not txn.transfer_pair_id or txn.amount >= 0:
        return None
    peer = session.get(Transaction, txn.transfer_pair_id)
    if peer is None or peer.status == TxnStatus.synced:
        return None
    return _actual_account_name(peer.source_account)


def _to_batch_txn(txn: Transaction, session=None) -> BatchTransaction:
    has_splits = len(txn.splits) > 0
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
        category_name=None if has_splits else txn.category,
        transfer_to_actual_account=_transfer_dest(txn, session),
        subtransactions=[
            BatchSubtransaction(amount=split.amount, category_name=split.category, notes=split.notes)
            for split in txn.splits
        ],
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
    session=None,   # para resolver el destino de las patas de transferencia
) -> BatchArtifact:
    """Agrupa por cuenta (importTransactions opera por cuenta) preservando el orden."""
    groups: "OrderedDict[str, list[Transaction]]" = OrderedDict()
    for t in txns:
        groups.setdefault(t.source_account, []).append(t)

    accounts = [
        BatchAccountGroup(
            actual_account_name=_actual_account_name(source_account),
            source_account_name=source_account,
            transactions=[_to_batch_txn(t, session) for t in rows],
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
    out_dir = artifacts_dir or str(settings.resolved_artifacts_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"batch-{artifact.batch_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        # model_dump con mode='json' serializa date/Decimal a string legible.
        json.dump(artifact.model_dump(mode="json"), f, ensure_ascii=False, indent=2)
    return path
