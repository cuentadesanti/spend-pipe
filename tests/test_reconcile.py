"""Tests de reconciliación: matching por niveles, claims únicos, adopción/rechazo."""
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from spend_pipe.ingest import ingest_file
from spend_pipe.models import ActualMirror, Base, Transaction, TxnStatus
from spend_pipe.reconcile import adopt, reconcile_import, reject_match

# Archivo entrante: 4 filas de la tarjeta Openbank (destino: Openbank Nómina (EUR)).
CARD_CSV = """Date,Payee,Memo,Amount
2026-02-06,Apple pay: BULEVAR,, -51.30
2026-02-14,MIYAKO SUSHI BAR,, -19.00
2026-03-10,OPEN BANK COMISION,, -0.78
2026-04-01,COMPRA NUEVA SIN LEGACY,, -33.00
"""


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)()


def _mirror(s, **kw):
    row = ActualMirror(
        actual_txn_id=kw.pop("actual_txn_id"),
        account_name=kw.pop("account_name", "Openbank Nómina (EUR)"),
        date=kw.pop("date"),
        amount_cents=kw.pop("amount_cents"),
        payee_name=kw.pop("payee_name", "legacy payee"),
        category_name=kw.pop("category_name", "Gastos variables / Ocio"),
        imported_id=kw.pop("imported_id", "Openbank Nómina *8579|Movimientos de Cuenta.xls|42"),
        **kw,
    )
    s.add(row)
    return row


def _ingest_card(s, tmp_path):
    f = tmp_path / "card.csv"
    f.write_text(CARD_CSV, encoding="utf-8")
    # Parser al vuelo con el mismo source_account que la tarjeta real.
    from spend_pipe.ingest import ingest_with_parser
    from spend_pipe.parsers.csv_generic import CsvColumnMap, GenericCsvParser
    parser = GenericCsvParser(
        "openbank-tdc", "Openbank Tarjeta", "EUR",
        CsvColumnMap(date="Date", payee="Payee", amount="Amount", memo="Memo"),
    )
    return ingest_with_parser(s, str(f), parser)


def test_tiers_classify_correctly(tmp_path):
    s = _session()
    # legacy exacta (fecha+monto) → nivel 2
    _mirror(s, actual_txn_id="leg-1", date=date(2026, 2, 6), amount_cents=-5130,
            payee_name="BULEVAR MADRID", category_name="Gastos variables / Restaurantes")
    # legacy con fecha corrida 2 días → nivel 3
    _mirror(s, actual_txn_id="leg-2", date=date(2026, 2, 16), amount_cents=-1900)
    # legacy con id spendpipe → invisible al matching difuso (es nuestra)
    _mirror(s, actual_txn_id="leg-3", date=date(2026, 3, 10), amount_cents=-78,
            imported_id="spendpipe:otra-cosa:2026-03-10:-78:aaaa:0")
    s.commit()

    r = _ingest_card(s, tmp_path)
    assert r.added == 4

    report = reconcile_import(s, _import_id(s), apply=False)
    assert report.tier2_adoptable == 1     # BULEVAR
    assert report.tier3_review == 1        # MIYAKO (±2 días)
    assert report.tier4_new == 2           # comisión (legacy es spendpipe) + compra nueva
    assert report.tier1_already_ours == 0


def test_tier1_when_our_id_already_in_actual(tmp_path):
    s = _session()
    r = _ingest_card(s, tmp_path)
    # Simular que la primera fila ya fue empujada: su imported_id está en el espejo.
    first = s.scalars(select(Transaction).order_by(Transaction.date)).first()
    _mirror(s, actual_txn_id="pushed-1", date=first.date,
            amount_cents=-5130, imported_id=first.imported_id)
    s.commit()

    report = reconcile_import(s, _import_id(s), apply=True)
    assert report.tier1_already_ours == 1
    s.refresh(first)
    assert first.status == TxnStatus.synced
    assert first.sync_origin == "push"
    assert first.actual_txn_id == "pushed-1"


def test_mirror_row_claimed_only_once(tmp_path):
    s = _session()
    # UNA sola legacy de -19.00, pero el archivo trae dos filas de -19.00 mismo día.
    _mirror(s, actual_txn_id="leg-solo", date=date(2026, 2, 14), amount_cents=-1900)
    s.commit()
    f = tmp_path / "dup.csv"
    f.write_text(
        "Date,Payee,Memo,Amount\n"
        "2026-02-14,CAFE UNO,, -19.00\n"
        "2026-02-14,CAFE DOS,, -19.00\n",
        encoding="utf-8",
    )
    from spend_pipe.ingest import ingest_with_parser
    from spend_pipe.parsers.csv_generic import CsvColumnMap, GenericCsvParser
    parser = GenericCsvParser(
        "openbank-tdc", "Openbank Tarjeta", "EUR",
        CsvColumnMap(date="Date", payee="Payee", amount="Amount", memo="Memo"),
    )
    ingest_with_parser(s, str(f), parser)

    report = reconcile_import(s, _import_id(s), apply=False)
    # Solo UNA puede adoptar la legacy; la otra es nueva (no hay segunda legacy).
    assert report.tier2_adoptable == 1
    assert report.tier4_new == 1


def test_adopt_and_reject(tmp_path):
    s = _session()
    _mirror(s, actual_txn_id="leg-1", date=date(2026, 2, 6), amount_cents=-5130,
            category_name="Gastos variables / Restaurantes")
    _mirror(s, actual_txn_id="leg-2", date=date(2026, 2, 16), amount_cents=-1900)
    s.commit()
    _ingest_card(s, tmp_path)
    reconcile_import(s, _import_id(s), apply=True)

    bulevar = s.scalars(select(Transaction).where(Transaction.raw_payee.contains("BULEVAR"))).one()
    miyako = s.scalars(select(Transaction).where(Transaction.raw_payee.contains("MIYAKO"))).one()

    # nivel 2 → candidato listo; adoptar deja synced/adopted SIN push.
    assert bulevar.match_tier == 2 and bulevar.match_candidate_id
    mirror = adopt(s, bulevar)
    assert bulevar.status == TxnStatus.synced
    assert bulevar.sync_origin == "adopted"
    assert bulevar.actual_txn_id == "leg-1"
    assert mirror.category_name == "Gastos variables / Restaurantes"  # la legacy gana

    # nivel 3 → needs_review con hint; rechazar la deja como nueva.
    assert miyako.match_tier == 3 and miyako.status == TxnStatus.needs_review
    reject_match(s, miyako)
    assert miyako.match_candidate_id is None
    assert miyako.match_tier == 4


def test_reconcile_is_idempotent(tmp_path):
    s = _session()
    _mirror(s, actual_txn_id="leg-1", date=date(2026, 2, 6), amount_cents=-5130)
    s.commit()
    _ingest_card(s, tmp_path)
    imp = _import_id(s)

    r1 = reconcile_import(s, imp, apply=True)
    bulevar = s.scalars(select(Transaction).where(Transaction.raw_payee.contains("BULEVAR"))).one()
    adopt(s, bulevar)
    s.commit()

    # Segunda pasada: la adoptada ya no participa (tiene actual_txn_id).
    r2 = reconcile_import(s, imp, apply=True)
    assert r2.tier2_adoptable == 0
    assert r2.total == r1.total - 1


def _import_id(s) -> str:
    from spend_pipe.models import Import
    return s.scalars(select(Import)).first().id
