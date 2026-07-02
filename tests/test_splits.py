from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spend_pipe.export import build_batch_artifact
from spend_pipe.models import Base, Batch, Import, Transaction
from spend_pipe.splits import SplitDraft, replace_splits, validate_splits


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)()


def _txn(amount=Decimal("-500.00"), category="Gastos variables / Salidas"):
    s = _session()
    imp = Import(source_bank="bbva", source_file="x.csv", file_hash="h", format="csv")
    s.add(imp)
    s.flush()
    txn = Transaction(
        import_id=imp.id,
        source_bank="bbva",
        source_account="BBVA Cuenta Digital",
        format="csv",
        date=datetime(2026, 7, 2, tzinfo=timezone.utc).date(),
        amount=amount,
        currency="MXN",
        raw_payee="COMPRA TEST",
        payee="Compra Test",
        category=category,
        imported_id="spendpipe:test",
    )
    s.add(txn)
    s.flush()
    return s, txn


def test_validate_splits_requires_exact_sum():
    _, txn = _txn()
    drafts = [
        SplitDraft(amount=Decimal("-200.00"), category="Gastos variables / Super"),
        SplitDraft(amount=Decimal("-299.99"), category="Gastos variables / Hogar"),
    ]
    errors = validate_splits(txn, drafts)
    assert any("suman -499.99" in err for err in errors)


def test_validate_splits_rejects_transfer():
    _, txn = _txn()
    txn.is_transfer = True
    drafts = [
        SplitDraft(amount=Decimal("-250.00"), category="A / Uno"),
        SplitDraft(amount=Decimal("-250.00"), category="A / Dos"),
    ]
    errors = validate_splits(txn, drafts)
    assert "Las transferencias no admiten splits." in errors


def test_export_embeds_subtransactions_and_clears_parent_category():
    s, txn = _txn(category="Gastos variables / Salidas")
    replace_splits(
        txn,
        [
            SplitDraft(amount=Decimal("-300.00"), category="Gastos variables / Super", notes="mandado"),
            SplitDraft(amount=Decimal("-200.00"), category="Gastos variables / Hogar"),
        ],
    )
    batch = Batch(approved_by="t", approved_at=datetime.now(timezone.utc))
    s.add(batch)
    s.flush()
    txn.batch_id = batch.id
    s.commit()

    art = build_batch_artifact(batch, [txn], approved_by="t", session=s)
    exported = art.accounts[0].transactions[0]
    assert exported.category_name is None
    assert [split.category_name for split in exported.subtransactions] == [
        "Gastos variables / Super",
        "Gastos variables / Hogar",
    ]
    assert [split.amount for split in exported.subtransactions] == [
        Decimal("-300.00"),
        Decimal("-200.00"),
    ]
