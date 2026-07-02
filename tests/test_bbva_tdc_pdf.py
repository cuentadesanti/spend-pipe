"""Tests del parser BBVA TDC PDF: semántica de signo de tarjeta y fixtures reales."""
import os
from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers.bbva_tdc_pdf import _money, _parse_full_date

REAL_JUN = os.path.expanduser("~/Downloads/file 2.pdf")
REAL_ABR = os.path.expanduser("~/Downloads/file 3.pdf")


def test_full_date_spanish_months():
    assert _parse_full_date("05-may-2026") == date(2026, 5, 5)
    assert _parse_full_date("02-jun-2026") == date(2026, 6, 2)
    assert _parse_full_date("no-fecha") is None
    assert _parse_full_date("05-xxx-2026") is None


def test_money_strips_currency_furniture():
    assert _money("$25,241.80") == Decimal("25241.80")
    assert _money("+$532.00") == Decimal("532.00")


@pytest.mark.skipif(not os.path.exists(REAL_JUN), reason="PDF real no disponible")
def test_real_june_statement_matches_handmade_csv():
    """El estado de junio debe calzar 1:1 con el bbva-tdc-junio.csv hecho a mano."""
    from spend_pipe.parsers import get_parser

    txns = get_parser("bbva-tdc", "pdf").parse(REAL_JUN)  # reconcilia adentro
    assert len(txns) == 6
    by_key = {(t.date.isoformat(), t.raw_payee.split(" ;")[0]): t.amount for t in txns}
    # '+' del estado = cargo → negativo en schema; '-' (pago BMOVIL) → positivo.
    assert by_key[("2026-05-05", "AT T CR")] == Decimal("-532.00")
    assert by_key[("2026-05-15", "BMOVIL.PAGO TDC")] == Decimal("25241.80")
    assert by_key[("2026-05-26", "UBER RIDE")] == Decimal("-70.00")
    # Detalle de FX en notes.
    fx = [t for t in txns if "TIPO DE CAMBIO" in (t.notes or "")]
    assert len(fx) == 2  # OpenAI y Patreon (USD)


@pytest.mark.skipif(not os.path.exists(REAL_ABR), reason="PDF real no disponible")
def test_real_april_statement_reconciles_with_points_credit():
    """Abril trae un abono que NO es pago (USO DE PUNTOS): el signo explícito lo cubre."""
    from spend_pipe.parsers import get_parser

    txns = get_parser("bbva-tdc", "pdf").parse(REAL_ABR)
    assert len(txns) == 9
    puntos = [t for t in txns if "PUNTOS" in t.raw_payee]
    assert puntos and puntos[0].amount == Decimal("1289.30")   # abono → positivo
    aero = [t for t in txns if t.raw_payee.startswith("AEROMEXICO")]
    assert aero and aero[0].amount == Decimal("-24120.00")     # cargo → negativo
