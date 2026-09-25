from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers import get_parser
from spend_pipe.schema import CommonTransaction
from spend_pipe.statement import ParsedFile, StatementMeta, parse_statement


def _txn(day: int) -> CommonTransaction:
    return CommonTransaction(
        source_bank="x", source_account="X", format="csv", date=date(2026, 9, day),
        amount=Decimal("-1.00"), currency="EUR", raw_payee="p",
    )


def test_parser_without_metadata_gets_period_from_dates(tmp_path):
    path = tmp_path / "tdc.csv"
    path.write_text("Date,Payee,Memo,Amount\n2026-09-03,OXXO,,-10.00\n2026-09-01,UBER,,-5.50\n")
    parsed = parse_statement(get_parser("bbva-tdc", "csv"), str(path))
    assert parsed.meta == StatementMeta()
    assert len(parsed.transactions) == 2
    assert parsed.period() == (date(2026, 9, 1), date(2026, 9, 3))


def test_explicit_period_wins_over_dates():
    parsed = ParsedFile([_txn(5)], StatementMeta(period_from=date(2026, 9, 1), period_to=date(2026, 9, 30)))
    assert parsed.period() == (date(2026, 9, 1), date(2026, 9, 30))


def test_period_without_dates_or_meta_fails():
    with pytest.raises(ValueError):
        ParsedFile([]).period()
