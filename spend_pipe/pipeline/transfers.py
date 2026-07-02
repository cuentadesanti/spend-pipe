"""Matching de transferencias entre cuentas propias (pagos de tarjeta, traspasos).

El problema: un movimiento entre dos cuentas propias aparece como una pata en cada
estado (TDC: 'BMOVIL.PAGO TDC' +25,241.80 ↔ Cuenta Digital: 'PAGO TARJETA DE
CREDITO' −25,241.80). Empujadas como transacciones sueltas inflan ingresos/gastos;
deben viajar a Actual como UNA transferencia vinculada.

Matching determinista, sin heurísticas de texto:
  - montos exactamente opuestos y misma moneda
  - cuentas DESTINO en Actual distintas (post-ACCOUNT_MAP; una transferencia
    dentro de la misma cuenta no existe)
  - fechas a ≤ WINDOW_DAYS de distancia (los SPEI liquidan al día siguiente)
  - ninguna pata pendiente, duplicada, rechazada ni ya emparejada

Nota: 'SPEI DEVUELTO' (misma cuenta, monto opuesto) NO matchea porque exige
cuentas distintas — correcto, es una devolución, no una transferencia.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..export import actual_account_name
from ..models import Transaction, TxnStatus

WINDOW_DAYS = 3

_MATCHABLE = (TxnStatus.parsed, TxnStatus.normalized, TxnStatus.needs_review, TxnStatus.approved)


def match_transfers(session: Session) -> int:
    """Empareja patas opuestas en todo el staging. Idempotente: las ya emparejadas
    (transfer_pair_id) no se tocan. Devuelve cuántos pares nuevos se formaron."""
    candidates = session.scalars(
        select(Transaction).where(
            Transaction.is_duplicate.is_(False),
            Transaction.pending.is_(False),
            Transaction.transfer_pair_id.is_(None),
            Transaction.status.in_(_MATCHABLE),
        )
    ).all()

    negatives = sorted((t for t in candidates if t.amount < 0), key=lambda t: t.date)
    positives = [t for t in candidates if t.amount > 0]

    matched = 0
    for neg in negatives:
        neg_dest = actual_account_name(neg.source_account)
        best = None
        for pos in positives:
            if pos.transfer_pair_id is not None:
                continue
            if pos.amount != -neg.amount or pos.currency != neg.currency:
                continue
            if actual_account_name(pos.source_account) == neg_dest:
                continue
            if abs((pos.date - neg.date).days) > WINDOW_DAYS:
                continue
            # La pata más cercana en fecha gana.
            if best is None or abs((pos.date - neg.date).days) < abs((best.date - neg.date).days):
                best = pos
        if best is not None:
            neg.is_transfer = best.is_transfer = True
            neg.transfer_pair_id = best.id
            best.transfer_pair_id = neg.id
            matched += 1
    return matched


def unpair(session: Session, txn: Transaction) -> None:
    """Deshace un par (falso positivo marcado en el review): limpia ambas patas."""
    peer = session.get(Transaction, txn.transfer_pair_id) if txn.transfer_pair_id else None
    for t in (txn, peer):
        if t is not None:
            t.is_transfer = False
            t.transfer_pair_id = None
