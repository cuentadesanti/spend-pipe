"""Tests del parser BBVA-PDF: el candado de reconciliación y el fixture real.

El candado: un parser visualmente plausible que no cuadra contra el resumen del
estado debe FALLAR (ParseValidationError), nunca meter basura en silencio.
"""
import os
from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers.bbva_pdf import StatementSummary, validate_against_summary
from spend_pipe.parsers.errors import ParseValidationError
from spend_pipe.schema import CommonTransaction

REAL_PDF = os.path.expanduser("~/Downloads/file.pdf")


def _txn(amount: str) -> CommonTransaction:
    return CommonTransaction(
        source_bank="bbva-cuenta-digital", source_account="BBVA Cuenta Digital",
        format="pdf", date=date(2026, 5, 15), amount=Decimal(amount),
        currency="MXN", raw_payee="X",
    )


def _summary(**kw) -> StatementSummary:
    base = dict(
        abonos_total=Decimal("100.00"), abonos_count=1,
        cargos_total=Decimal("40.00"), cargos_count=1,
        saldo_anterior=Decimal("10.00"), saldo_final=Decimal("70.00"),
    )
    base.update(kw)
    return StatementSummary(**base)


def test_validation_passes_when_everything_reconciles():
    validate_against_summary([_txn("100.00"), _txn("-40.00")], _summary())


def test_validation_fails_on_wrong_count():
    with pytest.raises(ParseValidationError, match="conteo"):
        validate_against_summary([_txn("100.00")], _summary())  # falta el cargo


def test_validation_fails_on_wrong_sum():
    with pytest.raises(ParseValidationError, match="suma"):
        validate_against_summary([_txn("100.00"), _txn("-39.99")], _summary())


def test_validation_fails_on_balance_mismatch():
    with pytest.raises(ParseValidationError, match="saldo"):
        validate_against_summary(
            [_txn("100.00"), _txn("-40.00")], _summary(saldo_final=Decimal("999.99"))
        )


# ── Fixture real (se salta si el PDF no está en esta máquina) ────────────────
@pytest.mark.skipif(not os.path.exists(REAL_PDF), reason="PDF real no disponible")
def test_real_statement_parses_and_reconciles():
    from spend_pipe.parsers import get_parser

    txns = get_parser("bbva-cuenta-digital", "pdf").parse(REAL_PDF)  # valida adentro
    assert len(txns) == 7
    total = sum(t.amount for t in txns)
    assert total == Decimal("171033.71")
    assert sum(t.amount for t in txns if t.amount > 0) == Decimal("601917.81")
    assert sum(t.amount for t in txns if t.amount < 0) == Decimal("-430884.10")

    # El caso killer del signo-por-columna: mismo importe 400,000.00 como cargo
    # (SPEI ENVIADO) y como abono (SPEI DEVUELTO) el mismo día.
    speis = sorted(t.amount for t in txns if abs(t.amount) == Decimal("400000.00"))
    assert speis == [Decimal("-400000.00"), Decimal("400000.00")]

    # Año inferido del periodo (mayo y junio de 2026).
    assert {t.date.year for t in txns} == {2026}
    assert {t.date.month for t in txns} == {5, 6}
