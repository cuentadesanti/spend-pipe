"""Tests del motor de reglas: semántica pre/normal/post, renombres y gating."""
from datetime import date
from decimal import Decimal

from spend_pipe.models import Transaction, TxnStatus
from spend_pipe.pipeline.categorize import classify
from spend_pipe.pipeline.normalize import run_pipeline


def _txn(raw_payee: str, amount="-100.00") -> Transaction:
    return Transaction(
        date=date(2026, 6, 15), amount=Decimal(amount), currency="MXN", raw_payee=raw_payee
    )


def test_nomina_match_with_payee_rename():
    v = classify("PAGO DE NOMINA EMISORA 14249")
    assert v.category == "Ingresos / Nómina"
    assert v.payee_override == "Nómina"
    assert v.confidence == 1.0


def test_uber_vs_uber_eats():
    # UBER sin EATS → Transporte; con EATS gana la regla de Restaurantes (última que matchea).
    assert classify("UBER RIDE").category == "Gastos variables / Transporte"
    assert classify("UBER * EATS PENDING").category == "Gastos variables / Restaurantes"


def test_pre_stage_is_overridden_by_merchant_rule():
    # 'SANTIAGO SILVA' (pre) es regla amplia de persona; un comercio (normal) la pisa.
    assert classify("TRANSFERENCIA SANTIAGO SILVA").category == "Finanzas / Transferencias"
    assert classify("WELLHUB SANTIAGO SILVA").category == "Gastos variables / Salud"


def test_run_pipeline_applies_category_and_rename():
    t = run_pipeline(_txn("PAGO DE NOMINA EMISORA 14249", amount="201917.81"))
    assert t.category == "Ingresos / Nómina"
    assert t.payee == "Nómina"
    assert t.status == TxnStatus.normalized


def test_unmatched_merchant_goes_to_needs_review():
    t = run_pipeline(_txn("COMERCIO TOTALMENTE DESCONOCIDO SA"))
    assert t.category is None
    assert t.status == TxnStatus.needs_review   # se etiqueta a mano en el review


def test_openbank_spain_rules_still_hit():
    assert classify("MERCADONA MADRID").category == "Gastos variables / Súper"
    assert classify("RENFE VIAJEROS").category == "Gastos variables / Viajes"


def test_apple_pay_prefix_does_not_mean_subscription():
    # 'Apple pay: <comercio>' NO es una suscripción de Apple: se quita el prefijo
    # y el comercio real matchea su propia regla.
    assert classify("Apple pay: DISTRITO BURGE").category == "Gastos variables / Restaurantes"
    assert classify("Apple pay: SPUTNIK CLIMBI").category == "Gastos variables / Ocio"
    assert classify("Apple pay: EMPRESA MUNICI").category == "Gastos variables / Transporte"
    # Las suscripciones reales de Apple sí matchean (APPLE.COM/BILL).
    assert classify("APPLE.COM/BILL").category == "Gastos fijos / Suscripciones"
