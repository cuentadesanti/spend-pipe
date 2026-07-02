"""Web UI de spend-pipe.

Happy path mensual: subir CSV (o pegar filas manuales) → revisar/editar el import →
aprobar → se escribe el artefacto → push a Actual con el worker Node.

La CLI/TUI queda como herramienta auxiliar; esta UI es el camino principal.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, upgrade_to_head
from ..export import build_batch_artifact, write_artifact
from ..ingest import ingest_file, ingest_manual, ingest_with_parser
from ..models import Batch, BatchStatus, CsvMapping, Import, Transaction, TxnStatus
from ..parsers import available_parsers
from ..parsers.csv_generic import CsvColumnMap, GenericCsvParser
from ..parsers.sniff import detect_file, header_signature

BASE_DIR = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = str(BASE_DIR / "artifacts")
INBOX_DIR = BASE_DIR / "inbox"
NODE_PUSHER = BASE_DIR / "node-pusher"

app = FastAPI(title="spend-pipe")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Estados editables a mano en el review.
EDITABLE_STATUS = [TxnStatus.normalized, TxnStatus.needs_review, TxnStatus.approved, TxnStatus.rejected]


@app.on_event("startup")
def _startup() -> None:
    upgrade_to_head()  # Alembic como fuente de verdad del schema
    INBOX_DIR.mkdir(exist_ok=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Home ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    imports = db.scalars(select(Import).order_by(Import.created_at.desc())).all()
    # Conteos por status para cada import.
    counts: dict[str, dict[str, int]] = {}
    for imp in imports:
        rows = db.execute(
            select(Transaction.status, func.count())
            .where(Transaction.import_id == imp.id)
            .group_by(Transaction.status)
        ).all()
        counts[imp.id] = {s.value: n for s, n in rows}
    batches = db.scalars(select(Batch).order_by(Batch.created_at.desc())).all()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "imports": imports,
            "counts": counts,
            "batches": batches,
            "parsers": available_parsers(),
        },
    )


# ── Ingest: upload único con auto-detección ─────────────────────────────────
def _save_to_inbox(file: UploadFile) -> Path:
    data = file.file.read()
    dest = INBOX_DIR / (file.filename or "upload")
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() != hashlib.sha256(data).hexdigest():
        stem, suffix = dest.stem, dest.suffix
        dest = INBOX_DIR / f"{stem}-{hashlib.sha256(data).hexdigest()[:6]}{suffix}"
    dest.write_bytes(data)
    return dest


def _mapping_to_parser(m: CsvMapping) -> GenericCsvParser:
    return GenericCsvParser(
        source_bank=m.source_bank, source_account=m.source_account, currency=m.currency,
        colmap=CsvColumnMap(**json.loads(m.colmap_json)), delimiter=m.delimiter,
    )


@app.post("/upload")
def upload(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    dest = _save_to_inbox(file)
    det = detect_file(str(dest))

    # 1. Fuente conocida con parser → ingesta directa.
    if det.parser_available:
        result = ingest_file(db, str(dest), det.source_bank, det.format)
        return RedirectResponse(
            f"/imports/{result.import_id}?detected={det.label}", status_code=303
        )

    # 2. CSV cuyo layout ya "aprendimos" (mapeo guardado) → entra solo.
    if det.kind == "csv" and det.csv_headers:
        sig = header_signature(det.csv_headers)
        saved = db.scalars(select(CsvMapping).where(CsvMapping.header_signature == sig)).first()
        if saved is not None:
            result = ingest_with_parser(db, str(dest), _mapping_to_parser(saved))
            return RedirectResponse(
                f"/imports/{result.import_id}?detected=Mapeo guardado: {saved.name}",
                status_code=303,
            )

    # 3. Sin ingesta directa → pantalla de diagnóstico (con form de mapeo si es CSV).
    return templates.TemplateResponse(
        "detect.html",
        {"request": request, "det": det, "file_path": str(dest), "filename": dest.name},
    )


@app.post("/upload/map")
def upload_mapped(
    file_path: str = Form(...),
    source_bank: str = Form(...),
    source_account: str = Form(...),
    currency: str = Form("MXN"),
    col_date: str = Form(...),
    col_payee: str = Form(...),
    col_amount: str = Form(""),
    col_debit: str = Form(""),
    col_credit: str = Form(""),
    col_memo: str = Form(""),
    invert_sign: str = Form(""),
    delimiter: str = Form(","),
    save_mapping: str = Form(""),
    mapping_name: str = Form(""),
    db: Session = Depends(get_db),
):
    colmap = CsvColumnMap(
        date=col_date, payee=col_payee,
        amount=col_amount or None, debit=col_debit or None, credit=col_credit or None,
        memo=col_memo or None, invert_sign=invert_sign == "on",
    )
    parser = GenericCsvParser(
        source_bank=source_bank, source_account=source_account,
        currency=currency, colmap=colmap, delimiter=delimiter,
    )
    result = ingest_with_parser(db, file_path, parser)

    if save_mapping == "on":
        with open(file_path, encoding="utf-8-sig") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        from ..parsers.sniff import find_header_line
        hdr = lines[find_header_line(lines, delimiter)].split(delimiter)
        db.add(CsvMapping(
            name=mapping_name or f"{source_bank} · {source_account}",
            header_signature=header_signature(hdr),
            source_bank=source_bank, source_account=source_account, currency=currency,
            delimiter=delimiter,
            colmap_json=json.dumps({
                "date": colmap.date, "payee": colmap.payee, "amount": colmap.amount,
                "debit": colmap.debit, "credit": colmap.credit, "memo": colmap.memo,
                "invert_sign": colmap.invert_sign,
            }),
        ))
        db.commit()

    return RedirectResponse(
        f"/imports/{result.import_id}?detected=Mapeado a mano: {source_bank}", status_code=303
    )


@app.post("/manual")
def manual(
    source_bank: str = Form(...),
    source_account: str = Form(...),
    currency: str = Form("MXN"),
    pasted: str = Form(...),
    db: Session = Depends(get_db),
):
    result = ingest_manual(db, pasted, source_bank, source_account, currency)
    return RedirectResponse(f"/imports/{result.import_id}", status_code=303)


# ── Review de un import ──────────────────────────────────────────────────────
@app.get("/imports/{import_id}", response_class=HTMLResponse)
def import_detail(import_id: str, request: Request, db: Session = Depends(get_db), detected: str = ""):
    imp = db.get(Import, import_id)
    txns = db.scalars(
        select(Transaction).where(Transaction.import_id == import_id).order_by(Transaction.date)
    ).all()
    return templates.TemplateResponse(
        "import_detail.html",
        {
            "request": request,
            "imp": imp,
            "txns": txns,
            "statuses": [s.value for s in EDITABLE_STATUS],
            "detected": detected,
        },
    )


@app.post("/imports/{import_id}/save")
async def save_import(import_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    txns = db.scalars(select(Transaction).where(Transaction.import_id == import_id)).all()
    for t in txns:
        if (payee := form.get(f"payee_{t.id}")) is not None:
            t.payee = payee.strip() or None
        if (cat := form.get(f"category_{t.id}")) is not None:
            t.category = cat.strip() or None
        if (st := form.get(f"status_{t.id}")) in {s.value for s in EDITABLE_STATUS}:
            t.status = TxnStatus(st)
        t.is_duplicate = form.get(f"dup_{t.id}") == "on"
    db.commit()
    return RedirectResponse(f"/imports/{import_id}", status_code=303)


@app.post("/imports/{import_id}/approve")
def approve_import(import_id: str, db: Session = Depends(get_db)):
    imp = db.get(Import, import_id)
    # Elegibles: solo lo revisado y listo (normalized o approved) y no duplicado.
    # needs_review (ej. Openbank AUTORIZADO/pendientes) queda fuera hasta revisión explícita.
    txns = db.scalars(
        select(Transaction).where(
            Transaction.import_id == import_id,
            Transaction.status.in_([TxnStatus.normalized, TxnStatus.approved]),
            Transaction.is_duplicate.is_(False),
        )
    ).all()

    batch = Batch(status=BatchStatus.approved, approved_by="web", approved_at=datetime.now(timezone.utc))
    db.add(batch)
    db.flush()
    for t in txns:
        t.batch_id = batch.id
        t.status = TxnStatus.approved
    db.commit()

    artifact = build_batch_artifact(batch, list(txns), approved_by="web")
    write_artifact(artifact, ARTIFACTS_DIR)
    return RedirectResponse(f"/batches/{batch.id}", status_code=303)


# ── Batch + push ─────────────────────────────────────────────────────────────
@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: str, request: Request, db: Session = Depends(get_db), output: str = ""):
    batch = db.get(Batch, batch_id)
    txns = db.scalars(select(Transaction).where(Transaction.batch_id == batch_id).order_by(Transaction.date)).all()
    artifact_path = Path(ARTIFACTS_DIR) / f"batch-{batch_id}.json"
    return templates.TemplateResponse(
        "batch_detail.html",
        {
            "request": request,
            "batch": batch,
            "txns": txns,
            "artifact_path": artifact_path,
            "output": output,
        },
    )


@app.post("/batches/{batch_id}/push")
def push_batch(batch_id: str, request: Request, db: Session = Depends(get_db)):
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
        if proc.returncode == 0:
            batch = db.get(Batch, batch_id)
            batch.status = BatchStatus.pushed
            for t in db.scalars(select(Transaction).where(Transaction.batch_id == batch_id)).all():
                t.status = TxnStatus.synced
            db.commit()
    except Exception as e:  # noqa: BLE001
        output = f"No se pudo ejecutar el worker Node: {e}\n(¿Corriste `npm install` en node-pusher?)"
    return templates.TemplateResponse(
        "batch_detail.html",
        {
            "request": request,
            "batch": db.get(Batch, batch_id),
            "txns": db.scalars(select(Transaction).where(Transaction.batch_id == batch_id).order_by(Transaction.date)).all(),
            "artifact_path": artifact_path,
            "output": output,
        },
    )
