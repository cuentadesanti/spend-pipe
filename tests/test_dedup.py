"""Tests MVP2: dedup cross-import por (cuenta destino, fecha, monto)."""
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from spend_pipe.ingest import ingest_file
from spend_pipe.models import Base, Transaction, TxnStatus

# La misma compra de OpenAI por dos fuentes: el CSV la escribe corto, el PDF con sufijo.
CSV_A = """Date,Payee,Memo,Amount
2026-05-09,OPENAI *CHATGPT SUBSCR,, -345.93
"""
CSV_B = """Date,Payee,Memo,Amount
2026-05-09,OPENAI *CHATGPT SUBSCR ; Tarjeta Digital ***3054,, -345.93
2026-05-26,UBER RIDE,, -70.00
"""

# Dos compras idénticas el mismo día EN EL MISMO archivo: legítimas, no dup.
CSV_LEGIT = """Date,Payee,Memo,Amount
2026-05-05,STARBUCKS,, -70.00
2026-05-05,STARBUCKS,, -70.00
"""


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)()


def test_cross_import_same_txn_different_payee_flagged(tmp_path):
    a = tmp_path / "a.csv"; a.write_text(CSV_A, encoding="utf-8")
    b = tmp_path / "b.csv"; b.write_text(CSV_B, encoding="utf-8")
    s = _session()
    ingest_file(s, str(a), "bbva-tdc", "csv")
    r = ingest_file(s, str(b), "bbva-tdc", "csv")
    # El payee distinto hace que el imported_id NO choque… pero el dedup_hash sí.
    assert r.added == 2

    txns = s.scalars(select(Transaction).order_by(Transaction.created_at)).all()
    openai_b = [t for t in txns if "3054" in t.raw_payee][0]
    openai_a = [t for t in txns if t.raw_payee == "OPENAI *CHATGPT SUBSCR"][0]
    uber = [t for t in txns if "UBER" in t.raw_payee][0]

    assert openai_b.is_duplicate is True
    assert openai_b.duplicate_of == openai_a.id
    assert openai_b.status == TxnStatus.needs_review   # gateada del approve
    assert uber.is_duplicate is False                  # la nueva de verdad pasa limpia


def test_same_import_same_day_pair_not_flagged(tmp_path):
    f = tmp_path / "legit.csv"; f.write_text(CSV_LEGIT, encoding="utf-8")
    s = _session()
    r = ingest_file(s, str(f), "bbva-tdc", "csv")
    assert r.added == 2
    txns = s.scalars(select(Transaction)).all()
    assert all(t.is_duplicate is False for t in txns)  # mismo import → occ index, no dup


def test_dedup_hash_populated_at_ingest(tmp_path):
    f = tmp_path / "a.csv"; f.write_text(CSV_A, encoding="utf-8")
    s = _session()
    ingest_file(s, str(f), "bbva-tdc", "csv")
    t = s.scalars(select(Transaction)).one()
    assert t.dedup_hash and len(t.dedup_hash) == 16
