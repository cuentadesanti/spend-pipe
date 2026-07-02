"""Helpers de splits: validacion, parseo y serializacion.

Los splits viven siempre colgados de una transaccion padre: no tienen identidad propia
hacia Actual. El imported_id y la idempotencia pertenecen al padre.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import Transaction, TransactionSplit

CENT = Decimal("0.01")


@dataclass(frozen=True)
class SplitDraft:
    amount: Decimal
    category: str
    notes: str | None = None


def parse_split_amount(raw: str) -> Decimal:
    s = (raw or "").strip().replace(",", "")
    if not s:
        raise ValueError("Monto faltante en split.")
    try:
        return Decimal(s).quantize(CENT)
    except InvalidOperation as e:
        raise ValueError(f"Monto invalido en split: {raw!r}") from e


def serialize_splits(txn: Transaction) -> list[dict[str, str]]:
    return [
        {
            "amount": f"{split.amount:.2f}",
            "category": split.category,
            "notes": split.notes or "",
        }
        for split in txn.splits
    ]


def validate_splits(txn: Transaction, drafts: list[SplitDraft]) -> list[str]:
    if not drafts:
        return []

    errors = []
    if txn.is_transfer:
        errors.append("Las transferencias no admiten splits.")

    total = Decimal("0.00")
    for draft in drafts:
        total += draft.amount
        if not draft.category.strip():
            errors.append("Cada split necesita categoria.")
        if draft.amount == 0:
            errors.append("Los splits no pueden tener monto 0.")
        if txn.amount and ((draft.amount > 0) != (Decimal(txn.amount) > 0)):
            errors.append("Cada split debe conservar el signo del movimiento padre.")

    if total.quantize(CENT) != Decimal(txn.amount).quantize(CENT):
        errors.append(
            f"Los splits suman {total.quantize(CENT):.2f} y la transaccion es {Decimal(txn.amount).quantize(CENT):.2f}."
        )
    return errors


def replace_splits(txn: Transaction, drafts: list[SplitDraft]) -> None:
    txn.splits[:] = [
        TransactionSplit(
            position=index,
            amount=draft.amount.quantize(CENT),
            category=draft.category.strip(),
            notes=(draft.notes or "").strip() or None,
        )
        for index, draft in enumerate(drafts)
    ]
