"""Servidor MCP de spend-pipe: el pipeline como tools para claude.ai / ChatGPT.

Transporte streamable-HTTP stateless (lo que piden los conectores remotos de
claude.ai y ChatGPT), montado bajo un path secreto /mcp-<SPENDPIPE_MCP_SECRET>
en el mismo FastAPI (ver app.py). El path ES la autenticación (patrón Zapier MCP).

Reglas de diseño:
  - Toda tool abre su propia sesión de DB (no comparte estado con la web UI).
  - Las tools que ESCRIBEN en Actual (pushear, eliminar) piden confirmar=True
    explícito: la primera llamada devuelve un resumen de lo que haría.
  - Lecturas de saldos/movimientos salen del espejo local (actual_mirror);
    refrescar_espejo() lo re-descarga de Actual (tarda ~1-2 min).
"""
from __future__ import annotations

import hashlib
import shutil
import urllib.request
from datetime import date as Date
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import func, select

from .. import ai
from ..config import settings
from ..db import SessionLocal
from ..ingest import ingest_file
from ..models import ActualMirror, Import, Transaction, TxnStatus
from ..parsers.sniff import detect_file
from ..reconcile import adopt, mirror_size, reconcile_import, refresh_mirror
from .actions import approve_import_core, delete_import_core, push_batch_core

mcp = FastMCP(
    "spend-pipe",
    instructions=(
        "Pipeline de ingesta financiera de Santiago hacia Actual Budget. "
        "Flujo típico de un archivo: ingerir_desde_url → detalle_import → "
        "reconciliar → adoptar_existentes → clasificar_pendientes → "
        "aprobar_y_pushear(confirmar=true). Consultas de gasto/saldos: "
        "estado_cuentas, buscar_transacciones, resumen_por_categoria "
        "(usan el espejo local; refrescar_espejo si está viejo)."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # La protección anti-DNS-rebinding valida el header Host contra una allowlist
    # que por default solo trae localhost → 421 'Invalid Host header' detrás del
    # proxy de Railway. Se mantiene ACTIVA, con el dominio público en la allowlist.
    transport_security=TransportSecuritySettings(
        allowed_hosts=[
            "spend-pipe-production.up.railway.app",
            "localhost:*",
            "127.0.0.1:*",
        ],
    ),
)


# ── Consultas (espejo de Actual) ─────────────────────────────────────────────
@mcp.tool()
def estado_cuentas() -> dict:
    """Saldos y cobertura por cuenta en Actual (según el espejo local), fecha del
    último refresh del espejo, y qué hay pendiente en staging."""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                ActualMirror.account_name,
                func.count(),
                func.min(ActualMirror.date),
                func.max(ActualMirror.date),
                func.sum(ActualMirror.amount_cents),
            ).group_by(ActualMirror.account_name).order_by(ActualMirror.account_name)
        ).all()
        refreshed = db.execute(select(func.max(ActualMirror.refreshed_at))).scalar()
        pendientes = db.execute(
            select(Transaction.status, func.count())
            .where(Transaction.status.in_([TxnStatus.needs_review, TxnStatus.normalized, TxnStatus.approved]))
            .group_by(Transaction.status)
        ).all()
    return {
        "espejo_refrescado": str(refreshed) if refreshed else "nunca (usa refrescar_espejo)",
        "cuentas": [
            {
                "cuenta": r[0], "movimientos": r[1],
                "desde": str(r[2]), "hasta": str(r[3]),
                "saldo": round((r[4] or 0) / 100, 2),
            }
            for r in rows
        ],
        "staging_pendiente": {s.value: n for s, n in pendientes},
    }


@mcp.tool()
def buscar_transacciones(
    texto: str = "",
    cuenta: str = "",
    categoria: str = "",
    desde: str = "",
    hasta: str = "",
    limite: int = 50,
) -> list[dict]:
    """Busca movimientos en Actual (espejo local). texto matchea payee/notas
    (case-insensitive), cuenta/categoria matchean por substring, desde/hasta
    son fechas ISO (YYYY-MM-DD)."""
    q = select(ActualMirror).order_by(ActualMirror.date.desc()).limit(min(limite, 200))
    if texto:
        q = q.where(ActualMirror.payee_name.ilike(f"%{texto}%"))
    if cuenta:
        q = q.where(ActualMirror.account_name.ilike(f"%{cuenta}%"))
    if categoria:
        q = q.where(ActualMirror.category_name.ilike(f"%{categoria}%"))
    if desde:
        q = q.where(ActualMirror.date >= Date.fromisoformat(desde))
    if hasta:
        q = q.where(ActualMirror.date <= Date.fromisoformat(hasta))
    with SessionLocal() as db:
        rows = db.scalars(q).all()
        return [
            {
                "fecha": str(m.date), "monto": m.amount_cents / 100,
                "payee": m.payee_name, "categoria": m.category_name,
                "cuenta": m.account_name,
            }
            for m in rows
        ]


@mcp.tool()
def resumen_por_categoria(desde: str, hasta: str = "", cuenta: str = "") -> dict:
    """Total por categoría entre dos fechas ISO (hasta = hoy si se omite).
    Excluye transferencias vinculadas y padres de splits. Montos en la moneda
    de cada cuenta (ojo al mezclar cuentas MXN y EUR: filtra por cuenta)."""
    q = (
        select(ActualMirror.category_name, func.count(), func.sum(ActualMirror.amount_cents))
        .where(
            ActualMirror.date >= Date.fromisoformat(desde),
            ActualMirror.transfer_id.is_(None),
            ActualMirror.is_parent.is_(False),
        )
        .group_by(ActualMirror.category_name)
    )
    if hasta:
        q = q.where(ActualMirror.date <= Date.fromisoformat(hasta))
    if cuenta:
        q = q.where(ActualMirror.account_name.ilike(f"%{cuenta}%"))
    with SessionLocal() as db:
        rows = db.execute(q).all()
    return {
        "desde": desde, "hasta": hasta or "hoy", "cuenta": cuenta or "todas",
        "categorias": sorted(
            (
                {"categoria": cat or "(sin categoría)", "movimientos": n, "total": round((s or 0) / 100, 2)}
                for cat, n, s in rows
            ),
            key=lambda x: x["total"],
        ),
    }


@mcp.tool()
def refrescar_espejo() -> dict:
    """Re-descarga TODAS las transacciones de Actual al espejo local. Tarda 1-2
    minutos; úsalo antes de consultar saldos si el espejo está viejo."""
    with SessionLocal() as db:
        refresh_mirror(db)
        return {"espejo_filas": mirror_size(db)}


# ── Staging / imports ────────────────────────────────────────────────────────
@mcp.tool()
def listar_imports() -> list[dict]:
    """Imports en staging con desglose por status."""
    with SessionLocal() as db:
        imports = db.scalars(select(Import).order_by(Import.created_at.desc()).limit(20)).all()
        out = []
        for imp in imports:
            counts = db.execute(
                select(Transaction.status, func.count())
                .where(Transaction.import_id == imp.id)
                .group_by(Transaction.status)
            ).all()
            out.append({
                "import_id": imp.id, "archivo": imp.source_file, "banco": imp.source_bank,
                "creado": str(imp.created_at.date()),
                "status": {s.value: n for s, n in counts},
            })
        return out


@mcp.tool()
def detalle_import(import_id: str) -> dict:
    """Desglose de un import: status, niveles de reconciliación, filas que
    necesitan revisión (con su categoría sugerida si la hay)."""
    with SessionLocal() as db:
        imp = db.get(Import, import_id)
        if imp is None:
            return {"error": f"No existe el import {import_id}"}
        txns = db.scalars(select(Transaction).where(Transaction.import_id == import_id)).all()
        review = [
            {
                "txn_id": t.id, "fecha": str(t.date), "monto": float(t.amount),
                "payee": t.payee or t.raw_payee, "categoria_sugerida": t.category,
                "nivel_match": t.match_tier,
            }
            for t in txns if t.status == TxnStatus.needs_review
        ]
        from collections import Counter
        return {
            "import_id": imp.id, "archivo": imp.source_file, "banco": imp.source_bank,
            "filas": len(txns),
            "status": dict(Counter(t.status.value for t in txns)),
            "reconciliacion": dict(Counter(f"nivel {t.match_tier}" for t in txns if t.match_tier)),
            "necesitan_revision": review[:60],
        }


@mcp.tool()
def ingerir_desde_url(url: str, nombre_archivo: str = "") -> dict:
    """Descarga un archivo (extracto bancario) desde una URL y lo ingiere si el
    formato es conocido. Devuelve la detección y, si ingirió, el import_id para
    seguir con reconciliar/clasificar/aprobar."""
    inbox = settings.resolved_inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    nombre = nombre_archivo or url.rsplit("/", 1)[-1].split("?")[0] or "descarga.bin"
    dest = inbox / nombre
    with urllib.request.urlopen(url, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)

    det = detect_file(str(dest))
    info = {
        "archivo": nombre, "deteccion": det.label, "banco": det.source_bank,
        "confianza": det.confidence, "parser_disponible": det.parser_available,
    }
    if not det.parser_available:
        info["guia"] = det.guidance or "Formato sin parser directo: súbelo por la web UI para mapear columnas."
        return info
    with SessionLocal() as db:
        result = ingest_file(db, str(dest), det.source_bank, det.format)
        info["import_id"] = result.import_id
        info["siguiente_paso"] = "reconciliar(import_id) y luego revisar con detalle_import"
        return info


# ── Reconciliación y clasificación ───────────────────────────────────────────
@mcp.tool()
def reconciliar(import_id: str, refrescar: bool = False) -> dict:
    """Matchea el import contra lo que ya existe en Actual (niveles: 2=ya existe,
    3=dudoso, 4=nuevo). Con refrescar=true re-descarga el espejo primero (+1-2 min)."""
    with SessionLocal() as db:
        if refrescar or mirror_size(db) == 0:
            refresh_mirror(db)
        reconcile_import(db, import_id, apply=True)
        rows = db.execute(
            select(Transaction.match_tier, func.count())
            .where(Transaction.import_id == import_id)
            .group_by(Transaction.match_tier)
        ).all()
    niveles = {f"nivel {t or 'sin match'}": n for t, n in rows}
    return {
        "niveles": niveles,
        "siguiente_paso": "adoptar_existentes para los nivel 2; los nivel 3 se revisan en la web UI",
    }


@mcp.tool()
def adoptar_existentes(import_id: str) -> dict:
    """Adopta todas las filas nivel 2 (ya existen en Actual): quedan synced sin
    pushear, la categoría legacy gana."""
    with SessionLocal() as db:
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
        return {"adoptadas": len(txns)}


@mcp.tool()
def clasificar_pendientes(import_id: str) -> dict:
    """Sugiere categoría (IA) para las filas sin regla. No cambia el status:
    las needs_review siguen necesitando confirmación."""
    with SessionLocal() as db:
        if not ai.is_enabled(db):
            return {"error": "No hay API key de Anthropic configurada (ajustes de la web UI)"}
        txns = db.scalars(
            select(Transaction).where(
                Transaction.import_id == import_id,
                Transaction.category.is_(None),
                Transaction.is_transfer.is_(False),
                Transaction.is_duplicate.is_(False),
                Transaction.actual_txn_id.is_(None),
            )
        ).all()
        from .app import get_actual_categories  # lazy: evita import circular

        categories = [c["full_name"] for c in get_actual_categories() if c.get("full_name")]
        suggestions = ai.suggest_categories(list(txns), categories, session=db)
        applied = ai.apply_suggestions(list(txns), suggestions)
        db.commit()
        return {"sugeridas": applied, "sin_regla": len(txns)}


@mcp.tool()
def confirmar_revision(txn_ids: list[str], categoria: str = "") -> dict:
    """Confirma filas needs_review → listas para push (status normalized).
    Si se pasa categoria, se asigna a todas las filas dadas."""
    with SessionLocal() as db:
        n = 0
        for txn_id in txn_ids:
            t = db.get(Transaction, txn_id)
            if t is None or t.status != TxnStatus.needs_review:
                continue
            if categoria:
                t.category = categoria
            t.status = TxnStatus.normalized
            n += 1
        db.commit()
        return {"confirmadas": n}


# ── Escritura en Actual (piden confirmar=True) ───────────────────────────────
@mcp.tool()
def aprobar_y_pushear(import_id: str, confirmar: bool = False) -> dict:
    """Aprueba las filas listas (normalized/approved, no duplicadas) y las pushea
    a Actual. Con confirmar=false (default) solo devuelve el resumen de lo que
    haría; repite con confirmar=true para ejecutar."""
    with SessionLocal() as db:
        elegibles = db.scalars(
            select(Transaction).where(
                Transaction.import_id == import_id,
                Transaction.status.in_([TxnStatus.normalized, TxnStatus.approved]),
                Transaction.is_duplicate.is_(False),
            )
        ).all()
        if not elegibles:
            return {"error": "No hay filas elegibles (¿todo adoptado/synced o needs_review?)"}
        resumen = {
            "filas_a_pushear": len(elegibles),
            "suma": round(float(sum(t.amount for t in elegibles)), 2),
            "sin_categoria": sum(1 for t in elegibles if not t.category),
        }
        if not confirmar:
            resumen["nota"] = "Dry-run. Repite con confirmar=true para pushear a Actual."
            return resumen

        batch = approve_import_core(db, import_id, approved_by="mcp")
        ok, output = push_batch_core(db, batch.id)
        resumen.update({"batch_id": batch.id, "push_ok": ok, "salida": output[-1500:]})
        return resumen


@mcp.tool()
def eliminar_import(import_id: str, confirmar: bool = False) -> dict:
    """Borra un import equivocado del staging (nunca borra nada en Actual; se
    bloquea si hay filas ya pusheadas). Pide confirmar=true."""
    with SessionLocal() as db:
        imp = db.get(Import, import_id)
        if imp is None:
            return {"error": f"No existe el import {import_id}"}
        if not confirmar:
            n = db.execute(
                select(func.count()).where(Transaction.import_id == import_id)
            ).scalar()
            return {
                "archivo": imp.source_file, "filas": n,
                "nota": "Dry-run. Repite con confirmar=true para borrarlo del staging.",
            }
        error = delete_import_core(db, import_id)
        return {"error": error} if error else {"eliminado": import_id}
