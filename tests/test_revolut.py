"""Parser del CSV de Revolut (cabeceras en español o inglés)."""
import csv
import os
from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers import get_parser
from spend_pipe.parsers.errors import ParseValidationError
from spend_pipe.parsers.revolut import RevolutCsvParser
from spend_pipe.parsers.sniff import detect_csv_text

ES_HEADER = "Tipo,Producto,Fecha de inicio,Fecha de finalización,Descripción,Importe,Comisión,Divisa,Estado,Saldo"
EN_HEADER = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"
ROWS = [
    "Pago con tarjeta,Actual,2026-09-01 10:00:00,2026-09-02 09:00:00,Mercadona,-25.50,0.00,EUR,COMPLETADO,974.50",
    "Transferencia,Actual,2026-09-03 10:00:00,2026-09-04 09:00:00,Top-up from BBVA,850.00,0.00,EUR,COMPLETADO,1824.50",
    "Cambio,Actual,2026-09-05 10:00:00,2026-09-05 10:00:01,Exchanged to MXN,-100.00,1.00,EUR,COMPLETADO,1723.50",
    "Pago con tarjeta,Actual,2026-09-06 10:00:00,,Amazon,-9.99,0.00,EUR,PENDIENTE,",
]
REAL_CSV = os.path.expanduser("~/Downloads/revoluteur.csv")


def _write(tmp_path, lines, header=ES_HEADER):
    path = tmp_path / "revolut.csv"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return str(path)


def test_parses_completed_rows_with_net_amounts(tmp_path):
    txns = RevolutCsvParser().parse(_write(tmp_path, ROWS))
    assert [t.amount for t in txns] == [Decimal("-25.50"), Decimal("850.00"), Decimal("-101.00")]
    assert [t.date for t in txns] == [date(2026, 9, 2), date(2026, 9, 4), date(2026, 9, 5)]
    assert {t.source_account for t in txns} == {"Revolut EUR"}
    assert txns[0].raw_payee == "Mercadona"
    assert txns[0].notes == "Pago con tarjeta"


def test_statement_meta_from_balance_chain(tmp_path):
    parsed = RevolutCsvParser().parse_statement(_write(tmp_path, ROWS))
    assert parsed.meta.opening_balance == Decimal("1000.00")
    assert parsed.meta.ending_balance == Decimal("1723.50")
    assert parsed.period() == (date(2026, 9, 2), date(2026, 9, 5))


def test_english_headers(tmp_path):
    rows = [r.replace("COMPLETADO", "COMPLETED").replace("PENDIENTE", "PENDING") for r in ROWS]
    assert len(RevolutCsvParser().parse(_write(tmp_path, rows, EN_HEADER))) == 3


def test_broken_balance_chain_fails(tmp_path):
    rows = [ROWS[0], ROWS[1].replace("1824.50", "1824.00"), ROWS[2]]
    with pytest.raises(ParseValidationError, match="cadena de saldos"):
        RevolutCsvParser().parse(_write(tmp_path, rows))


def test_mixed_currencies_fail(tmp_path):
    rows = [ROWS[0], ROWS[1].replace(",EUR,", ",MXN,")]
    with pytest.raises(ParseValidationError, match="mezcla divisas"):
        RevolutCsvParser().parse(_write(tmp_path, rows))


def test_detection_and_registry():
    det = detect_csv_text("\n".join([ES_HEADER, *ROWS]))
    assert (det.source_bank, det.format, det.parser_available) == ("revolut", "csv", True)
    assert isinstance(get_parser("revolut", "csv"), RevolutCsvParser)


@pytest.mark.skipif(not os.path.exists(REAL_CSV), reason="CSV real de Revolut no disponible")
def test_real_export_balances():
    parsed = RevolutCsvParser().parse_statement(REAL_CSV)
    with open(REAL_CSV, encoding="utf-8-sig", newline="") as f:
        last = list(csv.DictReader(f))[-1]
    assert parsed.meta.ending_balance == Decimal(last["Saldo"])
    assert parsed.meta.opening_balance + sum(t.amount for t in parsed.transactions) == parsed.meta.ending_balance
