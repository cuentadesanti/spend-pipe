"""Tests de la capa de parsers: registry, CSV genérico y parser manual."""
from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers import get_parser
from spend_pipe.parsers.csv_generic import CsvColumnMap, GenericCsvParser
from spend_pipe.parsers.manual import ManualParser

BBVA_CSV = """Date,Payee,Memo,Amount
2026-05-05,AT T CR,, -532.00
2026-05-05,AT T CR,, -532.00
2026-05-09,OPENAI *CHATGPT SUBSCR,USD 20.00, -345.93
"""


def test_registry_resolves_configured_bank():
    p = get_parser("bbva-tdc", "csv")
    assert p.source_bank == "bbva-tdc"
    assert p.format == "csv"


def test_registry_unknown_raises():
    with pytest.raises(LookupError):
        get_parser("banco-fantasma", "csv")


def test_generic_csv_parses_common_schema(tmp_path):
    f = tmp_path / "bbva.csv"
    f.write_text(BBVA_CSV, encoding="utf-8")
    parser = GenericCsvParser(
        source_bank="bbva-tdc",
        source_account="BBVA TDC",
        currency="MXN",
        colmap=CsvColumnMap(date="Date", amount="Amount", payee="Payee", memo="Memo"),
    )
    txns = parser.parse(str(f))
    assert len(txns) == 3
    t0 = txns[0]
    assert t0.date == date(2026, 5, 5)
    assert t0.amount == Decimal("-532.00")   # tolera el espacio inicial ' -532.00'
    assert t0.raw_payee == "AT T CR"
    assert t0.currency == "MXN"
    assert t0.source_row == 2                 # fila 1 = header
    assert txns[2].notes == "USD 20.00"


def test_manual_parser_from_pasted_text():
    parser = ManualParser(source_bank="bbva", source_account="BBVA Cuenta Digital", currency="MXN")
    pasted = "date,amount,payee\n2026-06-15,-1234.56,Uber\n"
    txns = parser.parse_text(pasted)
    assert len(txns) == 1
    assert txns[0].format == "manual"
    assert txns[0].amount == Decimal("-1234.56")


def test_manual_parser_missing_column_raises():
    parser = ManualParser(source_bank="bbva", source_account="X", currency="MXN")
    with pytest.raises(ValueError):
        parser.parse_text("date,amount\n2026-06-15,-10.00\n")
