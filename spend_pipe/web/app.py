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
from ..pipeline.transfers import unpair
from ..reconcile import adopt, mirror_size, reconcile_import, refresh_mirror, reject_match
from ..models import ActualMirror
from ..splits import SplitDraft, parse_split_amount, replace_splits, serialize_splits, validate_splits

BASE_DIR = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = str(settings.resolved_artifacts_dir)
INBOX_DIR = settings.resolved_inbox_dir
NODE_PUSHER = BASE_DIR / "node-pusher"

app = FastAPI(title="spend-pipe")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Estados editables a mano en el review.
EDITABLE_STATUS = [TxnStatus.normalized, TxnStatus.needs_review, TxnStatus.approved, TxnStatus.rejected]


@app.on_event("startup")
def _startup() -> None:
    upgrade_to_head()  # Alembic como fuente de verdad del schema
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(exist_ok=True)
    settings.resolved_artifacts_dir.mkdir(exist_ok=True)
    # Pre-calentar las listas de Actual SIN bloquear el arranque (cura del 499:
    # el fetch tarda 10-60s; la primera request sirve fallback y esto la reemplaza).
    categories_cache.refresh_in_background()
    accounts_cache.refresh_in_background()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _import_txns(db: Session, import_id: str) -> list[Transaction]:
    return db.scalars(
        select(Transaction).where(Transaction.import_id == import_id).order_by(Transaction.date, Transaction.created_at)
    ).all()


def _render_import_detail(
    import_id: str,
    request: Request,
    db: Session,
    detected: str = "",
    split_errors: dict[str, str] | None = None,
    split_drafts: dict[str, list[dict[str, str]]] | None = None,
):
    imp = db.get(Import, import_id)
    txns = _import_txns(db, import_id)
    drafts = {t.id: serialize_splits(t) for t in txns}
    if split_drafts:
        drafts.update(split_drafts)
        
    categories = get_actual_categories()
    categories_by_group = {}
    for cat in categories:
        categories_by_group.setdefault(cat["group_name"], []).append(cat)

    # Reconciliación: candidatos legacy de las filas con match pendiente.
    candidates = {
        t.id: db.get(ActualMirror, t.match_candidate_id)
        for t in txns if t.match_candidate_id and t.actual_txn_id is None
    }
    recon = {
        "mirror_rows": mirror_size(db),
        "tier2": [t for t in txns if t.match_tier == 2 and t.id in candidates],
        "tier3": [t for t in txns if t.match_tier == 3 and t.id in candidates],
        "adopted": sum(1 for t in txns if t.sync_origin == "adopted"),
        "candidates": candidates,
    }

    return templates.TemplateResponse(
        request=request,
        name="import_detail.html",
        context={
            "request": request,
            "imp": imp,
            "txns": txns,
            "statuses": [s.value for s in EDITABLE_STATUS],
            "detected": detected,
            "split_errors": split_errors or {},
            "split_drafts": drafts,
            "categories_by_group": categories_by_group,
            "recon": recon,
        },
    )


def _split_rows_from_form(form, txn_id: str) -> list[dict[str, str]]:
    amounts = form.getlist(f"split_amount[{txn_id}][]")
    categories = form.getlist(f"split_category[{txn_id}][]")
    notes = form.getlist(f"split_notes[{txn_id}][]")
    size = max(len(amounts), len(categories), len(notes))
    rows = []
    for i in range(size):
        rows.append(
            {
                "amount": amounts[i] if i < len(amounts) else "",
                "category": categories[i] if i < len(categories) else "",
                "notes": notes[i] if i < len(notes) else "",
            }
        )
    return rows


def _apply_review_form(
    db: Session, txns: list[Transaction], form
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    split_errors: dict[str, str] = {}
    split_drafts: dict[str, list[dict[str, str]]] = {}

    for t in txns:
        if (payee := form.get(f"payee_{t.id}")) is not None:
            t.payee = payee.strip() or None
        if (cat := form.get(f"category_{t.id}")) is not None:
            t.category = cat.strip() or None
        if (st := form.get(f"status_{t.id}")) in {s.value for s in EDITABLE_STATUS}:
            t.status = TxnStatus(st)
        t.is_duplicate = form.get(f"dup_{t.id}") == "on"
        if t.is_transfer and form.get(f"transfer_{t.id}") != "on":
            unpair(db, t)

        raw_rows = _split_rows_from_form(form, t.id)
        visible_rows = [row for row in raw_rows if any(v.strip() for v in row.values())]
        split_drafts[t.id] = visible_rows
        drafts: list[SplitDraft] = []
        row_errors: list[str] = []
        for row in visible_rows:
            try:
                amount = parse_split_amount(row["amount"])
            except ValueError as e:
                row_errors.append(str(e))
                continue
            drafts.append(
                SplitDraft(
                    amount=amount,
                    category=(row["category"] or "").strip(),
                    notes=(row["notes"] or "").strip() or None,
                )
            )
        if row_errors:
            split_errors[t.id] = " ".join(row_errors)
            continue
        business_errors = validate_splits(t, drafts)
        if business_errors:
            split_errors[t.id] = " ".join(dict.fromkeys(business_errors))
            continue
        replace_splits(t, drafts)

    return split_errors, split_drafts


# Listas de Actual con cache stale-while-revalidate: las requests NUNCA se
# bloquean en el subprocess de Node (cura del 499); ver node_cache.py.
from .node_cache import NodeListCache

_FALLBACK_CATEGORIES = [
    {"name": "Comida fuera", "group_name": "Gastos variables"},
    {"name": "Suscripciones", "group_name": "Gastos fijos"},
    {"name": "Supermercado", "group_name": "Gastos variables"},
    {"name": "Transporte", "group_name": "Gastos variables"},
    {"name": "Luz / Electricidad", "group_name": "Servicios"},
    {"name": "Internet", "group_name": "Servicios"},
]

categories_cache = NodeListCache(
    "get_categories.js", BASE_DIR / ".categories_cache.json", NODE_PUSHER,
    fallback=_FALLBACK_CATEGORIES,
)
accounts_cache = NodeListCache(
    "get_accounts.js", BASE_DIR / ".accounts_cache.json", NODE_PUSHER,
)


def get_actual_categories() -> list[dict]:
    return categories_cache.get()


def get_actual_accounts() -> list[dict]:
    return accounts_cache.get()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, month: str = None, db: Session = Depends(get_db)):
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
    
    # Determinar fecha seleccionada (mes objetivo)
    import datetime
    now_dt = datetime.datetime.now()
    if month:
        try:
            chosen_date = datetime.datetime.strptime(month, "%Y-%m")
        except ValueError:
            chosen_date = now_dt
    else:
        chosen_date = now_dt

    chosen_year = chosen_date.year
    chosen_month = chosen_date.month

    # Calcular meses contiguos para navegacion
    # Primer dia del mes actual
    first_day_curr = chosen_date.replace(day=1)
    prev_month_dt = first_day_curr - datetime.timedelta(days=1)
    next_month_dt = (first_day_curr + datetime.timedelta(days=32)).replace(day=1)
    
    prev_month_str = prev_month_dt.strftime("%Y-%m")
    next_month_str = next_month_dt.strftime("%Y-%m")
    
    SPANISH_MONTHS = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
    }
    current_month_label = f"{SPANISH_MONTHS[chosen_month]} {chosen_year}"

    # Consultar transacciones de ese año y mes
    from sqlalchemy import extract
    rows_month = db.execute(
        select(Transaction.source_account, Transaction.status, func.count())
        .where(
            extract("year", Transaction.date) == chosen_year,
            extract("month", Transaction.date) == chosen_month
        )
        .group_by(Transaction.source_account, Transaction.status)
    ).all()

    # Agrupar counts por cuenta
    monthly_stats = {}
    for src_acc, status, count in rows_month:
        monthly_stats.setdefault(src_acc, {}).setdefault(status.value, 0)
        monthly_stats[src_acc][status.value] += count

    # Obtener cuentas reales de Actual (PikaPods)
    actual_accounts = get_actual_accounts()
    
    ACCOUNT_MAPPING_INFO = {
        "BBVA Cuenta Digital (MXN)": {
            "id": "bbva-cuenta-digital",
            "display_name": "BBVA Cuenta Digital",
            "source_accounts": ["BBVA Cuenta Digital"],
            "formats_label": "Formatos: PDF",
        },
        "BBVA TDC": {
            "id": "bbva-tdc",
            "display_name": "BBVA Tarjeta de Crédito",
            "source_accounts": ["BBVA TDC", "BBVA México — movimientos"],
            "formats_label": "Formatos: PDF / CSV / App",
        },
        "Openbank Nómina (EUR)": {
            "id": "openbank-tdc",
            "display_name": "Openbank Tarjeta",
            "source_accounts": ["Openbank Tarjeta"],
            "formats_label": "Formatos: XLS / HTML",
        }
    }
    
    grid_items = []
    for acc in actual_accounts:
        acc_name = acc["name"]
        if acc_name in ACCOUNT_MAPPING_INFO:
            info = ACCOUNT_MAPPING_INFO[acc_name]
            
            total_txns = 0
            needs_review = 0
            approved_or_synced = 0
            for src in info["source_accounts"]:
                stats = monthly_stats.get(src, {})
                needs_review += stats.get("needs_review", 0)
                approved_or_synced += stats.get("approved", 0) + stats.get("synced", 0) + stats.get("normalized", 0) + stats.get("paired", 0)
                total_txns += sum(stats.values())
                
            if total_txns == 0:
                status = "pending"
                status_label = "Pendiente"
            elif needs_review > 0:
                status = "pending"
                status_label = f"Pendiente ({needs_review} por revisar)"
            else:
                status = "ready"
                status_label = f"Al día ({approved_or_synced} txns)"

            # REGLA ESTRICTA DE UI (Cero IDs Técnicos): Obtener último import exitoso
            last_imp = db.scalars(
                select(Import)
                .join(Transaction)
                .where(Transaction.source_account.in_(info["source_accounts"]))
                .order_by(Import.created_at.desc())
                .limit(1)
            ).first()

            if last_imp:
                now_val = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                created_at = last_imp.created_at.replace(tzinfo=None) if last_imp.created_at.tzinfo else last_imp.created_at
                diff = now_val - created_at
                if diff.days == 0:
                    if diff.seconds < 3600:
                        time_str = "hace unos minutos" if diff.seconds < 120 else f"hace {diff.seconds // 60} minutos"
                    else:
                        time_str = f"hace {diff.seconds // 3600} horas"
                elif diff.days == 1:
                    time_str = "ayer"
                else:
                    time_str = f"hace {diff.days} días"

                fn = last_imp.source_file
                if "/" in fn:
                    fn = fn.split("/")[-1]
                if "\\" in fn:
                    fn = fn.split("\\")[-1]
                if len(fn) > 28:
                    fn = fn[:25] + "..."
                last_load_label = f"Última carga: {time_str} ({fn})"
            else:
                last_load_label = "Sin cargas registradas"
                
            grid_items.append({
                "id": info["id"],
                "name": info["display_name"],
                "actual_name": acc_name,
                "formats_label": info["formats_label"],
                "status": status,
                "status_label": status_label,
                "last_load_label": last_load_label,
            })
            
    # Garantizar que siempre se muestren las 3 cuentas minimas por si falla el API de Actual
    if not grid_items:
        for acc_name, info in ACCOUNT_MAPPING_INFO.items():
            grid_items.append({
                "id": info["id"],
                "name": info["display_name"],
                "actual_name": acc_name,
                "formats_label": info["formats_label"],
                "status": "pending",
                "status_label": "Pendiente",
                "last_load_label": "Sin cargas registradas",
            })
            
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "imports": imports,
            "counts": counts,
            "batches": batches,
            "grid_items": grid_items,
            "current_month_label": current_month_label,
            "prev_month_str": prev_month_str,
            "next_month_str": next_month_str,
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
        request=request,
        name="detect.html",
        context={"request": request, "det": det, "file_path": str(dest), "filename": dest.name},
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
    return _render_import_detail(import_id, request, db, detected=detected)


@app.post("/imports/{import_id}/save")
async def save_import(import_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    txns = _import_txns(db, import_id)
    split_errors, split_drafts = _apply_review_form(db, txns, form)
    if split_errors:
        return _render_import_detail(import_id, request, db, split_errors=split_errors, split_drafts=split_drafts)
    db.commit()
    return RedirectResponse(f"/imports/{import_id}", status_code=303)


# ── Reconciliación contra Actual (data histórica/legacy) ────────────────────
@app.post("/imports/{import_id}/reconcile")
def reconcile_endpoint(import_id: str, refresh: str = Form(""), db: Session = Depends(get_db)):
    """Matchea el import contra el espejo. refresh=on re-descarga el espejo primero."""
    if refresh == "on" or mirror_size(db) == 0:
        refresh_mirror(db)
    reconcile_import(db, import_id, apply=True)
    return RedirectResponse(f"/imports/{import_id}?detected=Reconciliado contra Actual", status_code=303)


@app.post("/imports/{import_id}/adopt-all")
def adopt_all(import_id: str, db: Session = Depends(get_db)):
    """Adopta todas las nivel 2 (fecha+monto exacto o payee casi idéntico)."""
    txns = db.scalars(
        select(Transaction).where(
            Transaction.import_id == import_id,
            Transaction.match_tier == 2,
            Transaction.match_candidate_id.isnot(None),
            Transaction.actual_txn_id.is_(None),
        )
    ).all()
    for t in txns:
        adopt(db, t)
    db.commit()
    return RedirectResponse(
        f"/imports/{import_id}?detected=Adoptadas {len(txns)} transacciones ya existentes en Actual",
        status_code=303,
    )


@app.post("/txns/{txn_id}/adopt")
def adopt_one(txn_id: str, db: Session = Depends(get_db)):
    t = db.get(Transaction, txn_id)
    adopt(db, t)
    db.commit()
    return RedirectResponse(f"/imports/{t.import_id}", status_code=303)


@app.post("/txns/{txn_id}/reject-match")
def reject_one(txn_id: str, db: Session = Depends(get_db)):
    t = db.get(Transaction, txn_id)
    reject_match(db, t)
    db.commit()
    return RedirectResponse(f"/imports/{t.import_id}", status_code=303)


@app.post("/imports/{import_id}/approve")
async def approve_import(import_id: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    txns_all = _import_txns(db, import_id)
    split_errors, split_drafts = _apply_review_form(db, txns_all, form)
    if split_errors:
        return _render_import_detail(import_id, request, db, split_errors=split_errors, split_drafts=split_drafts)

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

    to_push: list[Transaction] = []
    for t in txns:
        # Pata POSITIVA de una transferencia (peer no synced): la representa la
        # contraparte que Actual auto-crea al empujar la pata negativa → no se pushea.
        if t.is_transfer and t.transfer_pair_id and t.amount > 0:
            peer = db.get(Transaction, t.transfer_pair_id)
            if peer is not None and peer.status != TxnStatus.synced:
                t.status = TxnStatus.paired
                continue
        t.batch_id = batch.id
        t.status = TxnStatus.approved
        to_push.append(t)
    db.commit()

    artifact = build_batch_artifact(batch, to_push, approved_by="web", session=db)
    write_artifact(artifact, ARTIFACTS_DIR)
    
    redirect_url = f"/batches/{batch.id}"
    if request.query_params.get("auto_push") == "1":
        redirect_url += "?auto_push=1"
        
    return RedirectResponse(redirect_url, status_code=303)


# ── Batch + push ─────────────────────────────────────────────────────────────
@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: str, request: Request, db: Session = Depends(get_db), output: str = ""):
    batch = db.get(Batch, batch_id)
    txns = db.scalars(select(Transaction).where(Transaction.batch_id == batch_id).order_by(Transaction.date)).all()
    artifact_path = Path(ARTIFACTS_DIR) / f"batch-{batch_id}.json"
    return templates.TemplateResponse(
        request=request,
        name="batch_detail.html",
        context={
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
        request=request,
        name="batch_detail.html",
        context={
            "request": request,
            "batch": db.get(Batch, batch_id),
            "txns": db.scalars(select(Transaction).where(Transaction.batch_id == batch_id).order_by(Transaction.date)).all(),
            "artifact_path": artifact_path,
            "output": output,
        },
    )
