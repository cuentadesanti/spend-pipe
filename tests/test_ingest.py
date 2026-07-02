"""Tests de ingest: idempotencia a nivel archivo y a nivel fila."""
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from spend_pipe.ingest import ingest_file, ingest_manual
from spend_pipe.models import Base, Transaction

CSV_A = """Date,Payee,Memo,Amount
2026-05-05,STARBUCKS,, -70.00
2026-05-09,OPENAI *CHATGPT SUBSCR,, -345.93
"""

# Solapa la fila de OPENAI con CSV_A y agrega una nueva.
CSV_B = """Date,Payee,Memo,Amount
2026-05-09,OPENAI *CHATGPT SUBSCR,, -345.93
2026-05-26,UBER RIDE,, -70.00
"""


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)()


def _count(s):
    return s.scalar(select(func.count()).select_from(Transaction))


def test_ingest_file_adds_rows(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text(CSV_A, encoding="utf-8")
    s = _session()
    r = ingest_file(s, str(f), "bbva-tdc", "csv")
    assert r.added == 2
    assert r.reused_existing is False
    assert _count(s) == 2


def test_reingesting_same_file_is_idempotent(tmp_path):
    f = tmp_path / "a.csv"
    f.write_text(CSV_A, encoding="utf-8")
    s = _session()
    ingest_file(s, str(f), "bbva-tdc", "csv")
    r2 = ingest_file(s, str(f), "bbva-tdc", "csv")
    assert r2.reused_existing is True
    assert r2.added == 0
    assert _count(s) == 2  # no se duplicó


def test_overlapping_rows_are_skipped(tmp_path):
    a = tmp_path / "a.csv"; a.write_text(CSV_A, encoding="utf-8")
    b = tmp_path / "b.csv"; b.write_text(CSV_B, encoding="utf-8")
    s = _session()
    ingest_file(s, str(a), "bbva-tdc", "csv")
    r = ingest_file(s, str(b), "bbva-tdc", "csv")
    # b tiene 2 filas: una solapa (OPENAI) → skip, una nueva (UBER) → add.
    assert r.added == 1
    assert r.skipped_duplicates == 1
    assert _count(s) == 3


def test_ingest_manual(tmp_path):
    s = _session()
    r = ingest_manual(s, "date,amount,payee\n2026-06-15,-1234.56,Uber\n", "bbva", "BBVA Cuenta Digital", "MXN")
    assert r.added == 1
    assert _count(s) == 1
