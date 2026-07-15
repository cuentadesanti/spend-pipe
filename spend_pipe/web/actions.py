"""Acciones núcleo del pipeline, compartidas entre la web UI y el servidor MCP.

Una sola fuente de verdad para aprobar/pushear/eliminar: los endpoints HTTP y
las tools MCP son wrappers finos sobre estas funciones.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..export import build_batch_artifact, write_artifact
from ..models import Batch, BatchStatus, Import, Transaction, TxnStatus
from ..pipeline.transfers import unpair

BASE_DIR = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = str(settings.resolved_artifacts_dir)
NODE_PUSHER = BASE_DIR / "node-pusher"


def approve_import_core(db: Session, import_id: str, approved_by: str = "web") -> Batch:
    """Crea el batch con las filas listas (normalized/approved, no duplicadas) y
    escribe el artefacto para el worker Node. Las patas positivas de transferencias
    con peer sin sync quedan como 'paired' (las crea Actual al pushear la negativa)."""
    txns = db.scalars(
        select(Transaction).where(
            Transaction.import_id == import_id,
            Transaction.status.in_([TxnStatus.normalized, TxnStatus.approved]),
            Transaction.is_duplicate.is_(False),
        )
    ).all()

    batch = Batch(status=BatchStatus.approved, approved_by=approved_by, approved_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()

    to_push: list[Transaction] = []
    for t in txns:
        if t.is_transfer and t.transfer_pair_id and t.amount > 0:
            peer = db.get(Transaction, t.transfer_pair_id)
            if peer is not None and peer.status != TxnStatus.synced:
                t.status = TxnStatus.paired
                continue
        t.batch_id = batch.id
        t.status = TxnStatus.approved
        to_push.append(t)
    db.commit()

    artifact = build_batch_artifact(batch, to_push, approved_by=approved_by, session=db)
    write_artifact(artifact, ARTIFACTS_DIR)
    return batch


def push_batch_core(db: Session, batch_id: str) -> tuple[bool, str]:
    """Corre el worker Node con --commit. Si sale bien, marca batch y filas como synced."""
    artifact_path = Path(ARTIFACTS_DIR) / f"batch-{batch_id}.json"
    try:
        proc = subprocess.run(
            ["node", "push.js", str(artifact_path), "--commit"],
            cwd=str(NODE_PUSHER),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        if ok:
            batch = db.get(Batch, batch_id)
            batch.status = BatchStatus.pushed
            for t in db.scalars(select(Transaction).where(Transaction.batch_id == batch_id)).all():
                t.status = TxnStatus.synced
            db.commit()
        return ok, output
    except Exception as e:  # noqa: BLE001
        return False, f"No se pudo ejecutar el worker Node: {e}\n(¿Corriste `npm install` en node-pusher?)"


def delete_import_core(db: Session, import_id: str) -> str | None:
    """Borra el import y sus filas del staging. Devuelve None si se borró, o el
    motivo si está bloqueado. Nada llegó a Actual salvo que se haya pusheado:
    si hay filas synced-por-push se bloquea (habría que limpiar Actual primero).
    Las adoptadas solo pierden el claim (el espejo las vuelve a ofrecer); al borrar
    el Import se libera el file-hash → re-subir el archivo vuelve a procesarlo."""
    imp = db.get(Import, import_id)
    if imp is None:
        return "El import no existe"
    txns = db.scalars(select(Transaction).where(Transaction.import_id == import_id)).all()

    pushed = [t for t in txns if t.status == TxnStatus.synced and t.sync_origin == "push"]
    if pushed:
        return f"No se puede eliminar: {len(pushed)} filas ya están en Actual (pusheadas)"

    ids = [t.id for t in txns]
    for t in txns:
        if t.transfer_pair_id:
            unpair(db, t)
    if ids:
        db.execute(
            update(Transaction)
            .where(Transaction.duplicate_of.in_(ids))
            .values(duplicate_of=None, is_duplicate=False)
        )
    db.delete(imp)   # cascade delete-orphan borra sus transacciones (y splits)
    db.commit()
    return None
