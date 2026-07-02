"""Tests del transfer matching: pares reales, falsos amigos y el contrato de export."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from spend_pipe.export import build_batch_artifact
from spend_pipe.ingest import ingest_file, ingest_with_parser
from spend_pipe.models import Base, Batch, Transaction
from spend_pipe.parsers.csv_generic import CsvColumnMap, GenericCsvParser
from spend_pipe.pipeline.transfers import match_transfers, unpair

# El caso real: pago de tarjeta visto desde ambos lados.
TDC_CSV = """Date,Payee,Memo,Amount
2026-05-15,BMOVIL.PAGO TDC,, 25241.80
2026-05-26,UBER RIDE,, -70.00
"""
CUENTA_CSV = """Date,Payee,Memo,Amount
2026-05-15,PAGO TARJETA DE CREDITO,, -25241.80
2026-05-15,SPEI ENVIADO NAFIN,, -400000.00
2026-05-15,SPEI DEVUELTO NAFIN,, 400000.00
"""


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)()


def _cuenta_parser():
    return GenericCsvParser(
        "bbva-cuenta-digital", "BBVA Cuenta Digital", "MXN",
        CsvColumnMap(date="Date", payee="Payee", amount="Amount", memo="Memo"),
    )


def _ingest_both(s, tmp_path):
    a = tmp_path / "tdc.csv"; a.write_text(TDC_CSV, encoding="utf-8")
    b = tmp_path / "cta.csv"; b.write_text(CUENTA_CSV, encoding="utf-8")
    ingest_file(s, str(a), "bbva-tdc", "csv")
    ingest_with_parser(s, str(b), _cuenta_parser())


def test_card_payment_pair_matched_across_imports(tmp_path):
    s = _session()
    _ingest_both(s, tmp_path)
    txns = s.scalars(select(Transaction)).all()
    bmovil = next(t for t in txns if "BMOVIL" in t.raw_payee)
    pago = next(t for t in txns if "PAGO TARJETA" in t.raw_payee)
    assert bmovil.is_transfer and pago.is_transfer
    assert bmovil.transfer_pair_id == pago.id and pago.transfer_pair_id == bmovil.id


def test_same_account_return_not_matched(tmp_path):
    # SPEI ENVIADO/DEVUELTO: montos opuestos pero MISMA cuenta → devolución, no transfer.
    s = _session()
    _ingest_both(s, tmp_path)
    txns = s.scalars(select(Transaction)).all()
    enviado = next(t for t in txns if "ENVIADO" in t.raw_payee)
    devuelto = next(t for t in txns if "DEVUELTO" in t.raw_payee)
    assert enviado.is_transfer is False and devuelto.is_transfer is False
    # Y UBER (sin contraparte) tampoco.
    assert next(t for t in txns if "UBER" in t.raw_payee).is_transfer is False


def test_different_currency_not_matched(tmp_path):
    s = _session()
    eur = GenericCsvParser("x-eur", "Cuenta EUR", "EUR",
                           CsvColumnMap(date="Date", payee="Payee", amount="Amount"))
    mxn = GenericCsvParser("x-mxn", "Cuenta MXN", "MXN",
                           CsvColumnMap(date="Date", payee="Payee", amount="Amount"))
    a = tmp_path / "e.csv"; a.write_text("Date,Payee,Amount\n2026-05-15,ABONO,100.00\n", encoding="utf-8")
    b = tmp_path / "m.csv"; b.write_text("Date,Payee,Amount\n2026-05-15,CARGO,-100.00\n", encoding="utf-8")
    ingest_with_parser(s, str(a), eur)
    ingest_with_parser(s, str(b), mxn)
    assert all(t.is_transfer is False for t in s.scalars(select(Transaction)))


def test_outside_date_window_not_matched(tmp_path):
    s = _session()
    p1 = GenericCsvParser("x1", "Cuenta A", "MXN", CsvColumnMap(date="Date", payee="Payee", amount="Amount"))
    p2 = GenericCsvParser("x2", "Cuenta B", "MXN", CsvColumnMap(date="Date", payee="Payee", amount="Amount"))
    a = tmp_path / "a.csv"; a.write_text("Date,Payee,Amount\n2026-05-01,ABONO,500.00\n", encoding="utf-8")
    b = tmp_path / "b.csv"; b.write_text("Date,Payee,Amount\n2026-05-10,CARGO,-500.00\n", encoding="utf-8")
    ingest_with_parser(s, str(a), p1)
    ingest_with_parser(s, str(b), p2)
    assert all(t.is_transfer is False for t in s.scalars(select(Transaction)))


def test_export_marks_negative_leg_with_destination(tmp_path):
    s = _session()
    _ingest_both(s, tmp_path)
    pago = s.scalars(select(Transaction).where(Transaction.raw_payee.contains("PAGO TARJETA"))).one()
    batch = Batch(approved_by="t", approved_at=datetime.now(timezone.utc))
    s.add(batch); s.flush()
    art = build_batch_artifact(batch, [pago], approved_by="t", session=s)
    bt = art.accounts[0].transactions[0]
    assert bt.transfer_to_actual_account == "BBVA TDC"   # destino = cuenta del peer
    assert art.schema_version == "1.2"


def test_unpair_clears_both_legs(tmp_path):
    s = _session()
    _ingest_both(s, tmp_path)
    bmovil = s.scalars(select(Transaction).where(Transaction.raw_payee.contains("BMOVIL"))).one()
    unpair(s, bmovil)
    txns = s.scalars(select(Transaction)).all()
    assert all(t.is_transfer is False and t.transfer_pair_id is None for t in txns)
