"""Test del artefacto exportado: parse → pipeline → identidad → staging → batch JSON."""
import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from spend_pipe.export import build_batch_artifact, write_artifact
from spend_pipe.identity import assign_imported_ids
from spend_pipe.models import Base, Batch, Import, Transaction, TxnStatus
from spend_pipe.parsers.csv_generic import CsvColumnMap, GenericCsvParser
from spend_pipe.pipeline.normalize import run_pipeline

CSV = """Date,Payee,Memo,Amount
2026-05-05,STARBUCKS,, -70.00
2026-05-05,STARBUCKS,, -70.00
2026-05-09,OPENAI *CHATGPT SUBSCR,, -345.93
"""


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)()


def test_export_builds_grouped_artifact(tmp_path):
    f = tmp_path / "bbva.csv"
    f.write_text(CSV, encoding="utf-8")
    parser = GenericCsvParser(
        "bbva-tdc", "BBVA TDC", "MXN",
        CsvColumnMap(date="Date", amount="Amount", payee="Payee", memo="Memo"),
    )
    cts = parser.parse(str(f))
    ids = assign_imported_ids([(c.source_account, c.date.isoformat(), c.amount, c.raw_payee) for c in cts])

    s = _session()
    imp = Import(source_bank="bbva-tdc", source_file="bbva.csv", file_hash="h", format="csv")
    s.add(imp); s.flush()
    batch = Batch(); s.add(batch); s.flush()
    for ct, iid in zip(cts, ids):
        t = Transaction(
            import_id=imp.id, batch_id=batch.id,
            source_bank=ct.source_bank, source_account=ct.source_account, format=ct.format,
            date=ct.date, amount=ct.amount, currency=ct.currency,
            raw_payee=ct.raw_payee, notes=ct.notes,
            source_file=ct.source_file, source_row=ct.source_row, imported_id=iid,
        )
        run_pipeline(t)
        t.status = TxnStatus.approved  # simula la aprobación humana
        s.add(t)
    s.commit()

    txns = s.scalars(select(Transaction).where(Transaction.batch_id == batch.id)).all()
    art = build_batch_artifact(batch, list(txns), approved_by="santiago")

    # Una sola cuenta, agrupada.
    assert len(art.accounts) == 1
    grp = art.accounts[0]
    assert grp.actual_account_name == "BBVA TDC"
    assert grp.source_account_name == "BBVA TDC"
    assert len(grp.transactions) == 3

    # MVP1: payee_name es el CRUDO (para que las reglas de Actual sigan matcheando).
    star = [t for t in grp.transactions if t.metadata.raw_payee == "STARBUCKS"]
    assert all(t.payee_name == "STARBUCKS" for t in star)
    # Los dos Starbucks idénticos del mismo día NO colisionan: occ 0 y 1.
    occs = sorted(t.imported_id.rsplit(":", 1)[1] for t in star)
    assert occs == ["0", "1"]
    # Categoría null en MVP1.
    assert all(t.category_name is None for t in grp.transactions)
    assert art.schema_version == "1.1"

    # El archivo escrito es JSON válido y recargable.
    path = write_artifact(art, str(tmp_path))
    reloaded = json.loads(open(path, encoding="utf-8").read())
    assert reloaded["batch_id"] == batch.id
    assert reloaded["accounts"][0]["transactions"][0]["imported_id"].startswith("spendpipe:bbva-tdc:")
