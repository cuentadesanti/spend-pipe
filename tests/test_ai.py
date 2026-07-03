"""Tests del AI-categorizer: candados de lista cerrada, no-pisar, y ajustes en DB."""
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spend_pipe import ai
from spend_pipe.models import Base, Transaction, TxnStatus


def _txn(id_, payee, category=None):
    return Transaction(
        id=id_, date=date(2026, 6, 1), amount=Decimal("-10.00"), currency="EUR",
        raw_payee=payee, category=category, status=TxnStatus.needs_review,
    )


class FakeMessages:
    def __init__(self, suggestions):
        self._s = suggestions

    def parse(self, **kwargs):
        class R:
            parsed_output = ai.SuggestionBatch(suggestions=self._s)
        # el system debe llevar la lista cerrada
        assert "Categorías válidas" in kwargs["system"]
        return R()


class FakeClient:
    def __init__(self, suggestions):
        self.messages = FakeMessages(suggestions)


CATS = ["Gastos variables / Restaurantes", "Gastos fijos / Suscripciones"]


def test_valid_suggestion_applies_with_ai_confidence():
    txns = [_txn("t1", "REST DESCONOCIDO")]
    client = FakeClient([ai.Suggestion(txn_id="t1", category=CATS[0], reason="restaurante")])
    out = ai.suggest_categories(txns, CATS, client=client)
    applied = ai.apply_suggestions(txns, out)
    assert applied == 1
    assert txns[0].category == CATS[0]
    assert txns[0].confidence == ai.AI_CONFIDENCE          # < 1.0: origen IA auditable
    assert txns[0].status == TxnStatus.needs_review        # el humano confirma


def test_invented_category_is_discarded():
    txns = [_txn("t1", "X")]
    client = FakeClient([ai.Suggestion(txn_id="t1", category="Categoría Inventada / Nueva", reason="")])
    out = ai.suggest_categories(txns, CATS, client=client)
    assert out == {}   # lista cerrada: lo que no está en el catálogo se descarta


def test_never_overwrites_existing_category():
    txns = [_txn("t1", "UBER", category="Gastos variables / Transporte")]
    applied = ai.apply_suggestions(txns, {"t1": CATS[0]})
    assert applied == 0
    assert txns[0].category == "Gastos variables / Transporte"


def test_abstention_null_category():
    txns = [_txn("t1", "TRANSFERENCIA RARA")]
    client = FakeClient([ai.Suggestion(txn_id="t1", category=None, reason="ambiguo")])
    out = ai.suggest_categories(txns, CATS, client=client)
    assert out == {}


def test_settings_stored_in_db_and_masked():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(engine)()
    assert ai.is_enabled(s) is False
    ai.set_api_key(s, "sk-ant-api03-abcdefghijklmnop")
    ai.set_model(s, "claude-opus-4-8")
    s.commit()
    assert ai.is_enabled(s) is True
    assert ai.get_model(s) == "claude-opus-4-8"
    masked = ai.mask_key(ai.get_api_key(s))
    assert "abcdefghijkl" not in masked and masked.startswith("sk-ant-")
