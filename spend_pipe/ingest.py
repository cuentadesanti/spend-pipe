"""Paso de ingest: archivo/texto → filas de staging.

Convierte la salida de un parser (schema común) en filas `Transaction`, asignando
`imported_id` (necesita ver todas las filas del archivo para el índice de ocurrencia),
corriendo el pipeline determinista, y persistiendo. Idempotente en dos niveles:

  - nivel archivo: si ya se ingirió un archivo con el mismo hash, no se re-procesa.
  - nivel fila:    filas cuyo imported_id ya existe en staging se saltan.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .export import actual_account_name
from .identity import assign_imported_ids, dedup_hash
from .models import Import, ImportStatus, Transaction, TxnStatus
from .parsers import get_parser
from .parsers.manual import ManualParser
from .pipeline.normalize import run_pipeline
from .schema import CommonTransaction


@dataclass
class IngestResult:
    import_id: str
    parsed: int
    added: int
    skipped_duplicates: int
    needs_review: int
    reused_existing: bool = False   # True si el archivo ya se había ingerido (mismo hash)


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _persist(
    session: Session,
    import_: Import,
    commons: list[CommonTransaction],
) -> IngestResult:
    """Núcleo común: asigna identidad, corre pipeline, salta duplicados, persiste."""
    ids = assign_imported_ids(
        [(c.source_account, c.date.isoformat(), c.amount, c.raw_payee) for c in commons]
    )

    # Filas ya presentes en staging (ingest previo / otra fuente que solapa).
    existing = set(
        session.scalars(select(Transaction.imported_id).where(Transaction.imported_id.in_(ids))).all()
    )

    added = skipped = needs_review = 0
    for ct, iid in zip(commons, ids):
        if iid in existing:
            skipped += 1
            continue
        existing.add(iid)  # evita colisión si el mismo archivo repite una fila idéntica
        t = Transaction(
            import_id=import_.id,
            source_bank=ct.source_bank,
            source_account=ct.source_account,
            format=ct.format,
            source_file=ct.source_file,
            source_row=ct.source_row,
            date=ct.date,
            amount=ct.amount,
            currency=ct.currency,
            raw_payee=ct.raw_payee,
            notes=ct.notes,
            pending=ct.pending,
            imported_id=iid,
            dedup_hash=dedup_hash(
                actual_account_name(ct.source_account), ct.date.isoformat(), ct.amount
            ),
        )
        run_pipeline(t)
        _flag_cross_import_duplicate(session, import_.id, t)
        if t.status == TxnStatus.needs_review:
            needs_review += 1
        session.add(t)
        added += 1

    import_.status = ImportStatus.parsed
    session.commit()
    return IngestResult(
        import_id=import_.id,
        parsed=len(commons),
        added=added,
        skipped_duplicates=skipped,
        needs_review=needs_review,
    )


def _flag_cross_import_duplicate(session: Session, import_id: str, t: Transaction) -> None:
    """Marca sospechas de duplicado CROSS-IMPORT (MVP2).

    Un match de dedup_hash (cuenta destino + fecha + monto) contra una fila de OTRO
    import es la misma transacción real llegando por dos fuentes/archivos (payee
    escrito distinto → imported_id distinto → se colaría sin esto). Dentro del mismo
    import NO se marca: dos compras idénticas el mismo día son legítimas (occ index).
    La sospecha va a needs_review y queda excluida del approve hasta que la revises.
    """
    prior = session.scalars(
        select(Transaction).where(
            Transaction.dedup_hash == t.dedup_hash,
            Transaction.import_id != import_id,
            Transaction.is_duplicate.is_(False),
        )
    ).first()
    if prior is not None:
        t.is_duplicate = True
        t.duplicate_of = prior.id
        t.status = TxnStatus.needs_review


def ingest_file(session: Session, file_path: str, source_bank: str, format: str) -> IngestResult:
    """Ingiere un archivo usando el parser registrado para (source_bank, format)."""
    return ingest_with_parser(session, file_path, get_parser(source_bank, format))


def ingest_with_parser(session: Session, file_path: str, parser) -> IngestResult:
    """Ingiere un archivo con una instancia de parser concreta (registrada o construida
    al vuelo, ej. desde el form de mapeo de columnas del Web UI)."""
    file_hash = file_sha256(file_path)

    prior = session.scalars(select(Import).where(Import.file_hash == file_hash)).first()
    if prior is not None:
        # Mismo archivo ya ingerido: idempotente, no se re-procesa.
        return IngestResult(
            import_id=prior.id, parsed=0, added=0, skipped_duplicates=0,
            needs_review=0, reused_existing=True,
        )

    commons = parser.parse(file_path)

    import_ = Import(
        source_bank=parser.source_bank,
        source_file=file_path.rsplit("/", 1)[-1],
        file_hash=file_hash,
        format=parser.format,
        raw_file_path=file_path,
    )
    session.add(import_)
    session.flush()
    return _persist(session, import_, commons)


def ingest_manual(
    session: Session,
    pasted: str,
    source_bank: str,
    source_account: str,
    currency: str,
) -> IngestResult:
    """Ingiere filas pegadas a mano (puente para PDF de BBVA hasta MVP5)."""
    parser = ManualParser(source_bank=source_bank, source_account=source_account, currency=currency)
    commons = parser.parse_text(pasted)

    # El hash cubre el contenido pegado, así re-pegar lo mismo es idempotente a nivel archivo.
    file_hash = hashlib.sha256(pasted.strip().encode("utf-8")).hexdigest()
    prior = session.scalars(select(Import).where(Import.file_hash == file_hash)).first()
    if prior is not None:
        return IngestResult(
            import_id=prior.id, parsed=0, added=0, skipped_duplicates=0,
            needs_review=0, reused_existing=True,
        )

    import_ = Import(
        source_bank=source_bank,
        source_file="manual-paste",
        file_hash=file_hash,
        format="manual",
    )
    session.add(import_)
    session.flush()
    return _persist(session, import_, commons)
