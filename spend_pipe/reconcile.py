"""Reconciliación contra Actual: espejo local + matching por niveles + adopción.

El problema que resuelve: el dedup de spend-pipe (MVP2) solo compara contra su propio
staging. La data histórica que llegó a Actual por otra vía (imports legacy de
finanzas-ai, entradas manuales) es invisible, y un push de un archivo con historia
puede doble-contar (caso real: ~20% del archivo de tarjeta Openbank ya existía en
'Openbank Nómina (EUR)' vía el import legacy del extracto de cuenta).

Matching determinista por niveles (el payee NUNCA es clave — legacy lo escribió a
mano distinto seguro; solo es señal visual en el review):

  nivel 1  imported_id exacto en el espejo      → ya es nuestra → synced
  nivel 2  cuenta destino + monto + (fecha exacta, O fecha ±WINDOW con payee muy
           similar ≥ SIM_AUTO)                  → adoptar (auto)
  nivel 3  cuenta + monto, fecha ±WINDOW, payee dudoso → needs_review con hint
  nivel 4  sin match                            → nueva de verdad → flujo normal

La similitud de payee NO es clave de identidad: es desempate (dos legacy del mismo
día y monto → cada una va con el payee que más se le parece; bug real detectado con
datos: dos -20.00 del 26-01 quedaban cruzadas) y auto-confirmación del corrimiento
de fecha operación→liquidación (+1-3 días) que infla el nivel 3 (231/353 en el
archivo real; con payee casi idéntico tipo 'Apple pay: UBER *TRIP' ↔ 'Uber *Trip'
es la misma transacción, no hace falta humano).

Adopción = en vez de re-insertar: staging queda synced con el actual_txn_id legacy
(sync_origin='adopted'). El claim vive ENTERAMENTE en spend-pipe: verificado contra
Actual real (2026-07-03) que la API no permite re-escribir imported_id de una txn
existente (updateTransaction lo ignora, financial_id también, y el merge fuzzy de
importTransactions solo reclama txns sin id — con id distinto crea duplicado). La
convergencia es por actual_txn_id: una fila del espejo ya adoptada por staging queda
excluida de futuros matchings. La categoría legacy GANA (la puso un humano); la de
spend-pipe queda solo como sugerencia en el review.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from difflib import SequenceMatcher

from .export import actual_account_name
from .identity import amount_to_cents
from .models import ActualMirror, Transaction, TxnStatus
from .pipeline.categorize import strip_wallet_prefix

BASE_DIR = Path(__file__).resolve().parents[1]
NODE_PUSHER = BASE_DIR / "node-pusher"
WINDOW_DAYS = 3
SIM_AUTO = 0.72   # similitud de payee para auto-adoptar un match con fecha corrida


def payee_similarity(a: str | None, b: str | None) -> float:
    """Similitud [0..1] entre payees, tolerante a wallet-prefix, caso y espacios."""
    ca = " ".join(strip_wallet_prefix(a or "").upper().split())
    cb = " ".join(strip_wallet_prefix(b or "").upper().split())
    if not ca or not cb:
        return 0.0
    sim = SequenceMatcher(None, ca, cb).ratio()
    # Openbank trunca el comercio a ~14 chars en la tarjeta pero el extracto trae el
    # nombre completo ('GARITOHOSTER E' ↔ 'GARITOHOSTER EXPERTS S.L'): comparar
    # también el prefijo común (mínimo 8 chars para no inflar strings cortos).
    n = min(len(ca), len(cb))
    if n >= 8:
        sim = max(sim, SequenceMatcher(None, ca[:n], cb[:n]).ratio())
    return sim

# Estados de staging que participan de la reconciliación (lo ya resuelto no se toca).
_RECONCILABLE = (TxnStatus.parsed, TxnStatus.normalized, TxnStatus.needs_review)


# ── Espejo ───────────────────────────────────────────────────────────────────
def refresh_mirror(session: Session, timeout: int = 180) -> int:
    """Re-descarga TODO el espejo desde Actual (get_transactions.js). Devuelve filas."""
    proc = subprocess.run(
        ["node", "get_transactions.js"],
        cwd=str(NODE_PUSHER), capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"get_transactions.js falló: {proc.stderr[-500:]}")
    json_line = next(
        (l for l in proc.stdout.splitlines() if l.strip().startswith("[")), None
    )
    if json_line is None:
        raise RuntimeError("get_transactions.js no produjo JSON en stdout")
    rows = json.loads(json_line)

    # Reemplazo completo: el espejo es un cache derivado, no fuente de verdad.
    # Antes de borrar hay que soltar las referencias match_candidate_id (FK): las
    # filas ya resueltas (adoptadas → actual_txn_id) no lo necesitan, y las
    # pendientes lo recuperan al correr reconcile_import de nuevo tras el refresh.
    from sqlalchemy import update as _update

    from .models import Transaction as _Txn

    session.execute(
        _update(_Txn).where(_Txn.match_candidate_id.isnot(None)).values(match_candidate_id=None)
    )
    session.execute(delete(ActualMirror))
    for r in rows:
        session.add(ActualMirror(
            actual_txn_id=r["actual_txn_id"],
            account_name=r["account_name"],
            date=_parse_date(r["date"]),
            amount_cents=r["amount_cents"],
            payee_name=r.get("payee_name"),
            category_name=r.get("category_name"),
            imported_id=r.get("imported_id"),
            notes=r.get("notes"),
            is_parent=bool(r.get("is_parent")),
            transfer_id=r.get("transfer_id"),
        ))
    session.commit()
    return len(rows)


def _parse_date(s: str):
    from datetime import date
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


def mirror_size(session: Session) -> int:
    from sqlalchemy import func
    return session.scalar(select(func.count()).select_from(ActualMirror)) or 0


# ── Matching ─────────────────────────────────────────────────────────────────
@dataclass
class ReconcileReport:
    tier1_already_ours: int = 0
    tier2_adoptable: int = 0
    tier3_review: int = 0
    tier4_new: int = 0
    details: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.tier1_already_ours + self.tier2_adoptable + self.tier3_review + self.tier4_new


def reconcile_import(session: Session, import_id: str, apply: bool = False) -> ReconcileReport:
    """Matchea las filas de un import contra el espejo. Con apply=False solo reporta
    (dry-run); con apply=True marca los resultados en staging (nivel 2 queda como
    candidato aceptado pendiente de backfill; nivel 3 como needs_review con hint).

    Una fila del espejo se puede reclamar UNA sola vez por pasada: dos compras
    idénticas del archivo no pueden adoptar la misma legacy (la segunda cae a nivel 3).
    """
    txns = session.scalars(
        select(Transaction).where(
            Transaction.import_id == import_id,
            Transaction.status.in_(_RECONCILABLE),
            Transaction.actual_txn_id.is_(None),
        ).order_by(Transaction.date)
    ).all()

    report = ReconcileReport()
    claimed: set[str] = set()   # mirror ids reclamados en esta pasada

    # Filas de Actual ya adoptadas/vinculadas por CUALQUIER fila de staging: fuera
    # de los candidatos (la convergencia id-based vive acá, no en Actual — su API
    # no permite backfillear imported_id sobre txns existentes).
    already_linked = set(
        session.scalars(
            select(Transaction.actual_txn_id).where(Transaction.actual_txn_id.isnot(None))
        ).all()
    )

    for t in txns:
        dest = actual_account_name(t.source_account)
        cents = amount_to_cents(t.amount)

        # nivel 1: nuestro imported_id ya está en Actual (push previo por otra vía).
        m1 = session.scalars(
            select(ActualMirror).where(ActualMirror.imported_id == t.imported_id)
        ).first()
        if m1 is not None:
            report.tier1_already_ours += 1
            report.details.append(_detail(t, 1, m1))
            if apply:
                t.actual_txn_id = m1.actual_txn_id
                t.sync_origin = "push"
                t.status = TxnStatus.synced
                t.match_tier = 1
            continue

        # candidatos por cuenta+monto (sin las que ya son de spend-pipe ni las reclamadas).
        candidates = session.scalars(
            select(ActualMirror).where(
                ActualMirror.account_name == dest,
                ActualMirror.amount_cents == cents,
            )
        ).all()
        candidates = [
            c for c in candidates
            if c.id not in claimed
            and c.actual_txn_id not in already_linked
            and not (c.imported_id or "").startswith("spendpipe:")
        ]

        # Mejor candidato por similitud de payee primero (desempata legacy gemelas
        # del mismo día/monto), después por cercanía de fecha.
        exact = sorted(
            (c for c in candidates if c.date == t.date),
            key=lambda c: -payee_similarity(t.raw_payee, c.payee_name),
        )
        near = sorted(
            (c for c in candidates if c.date != t.date and abs((c.date - t.date).days) <= WINDOW_DAYS),
            key=lambda c: (-payee_similarity(t.raw_payee, c.payee_name), abs((c.date - t.date).days)),
        )

        if exact:
            m = exact[0]
            claimed.add(m.id)
            report.tier2_adoptable += 1
            report.details.append(_detail(t, 2, m))
            if apply:
                t.match_candidate_id = m.id
                t.match_tier = 2
        elif near and payee_similarity(t.raw_payee, near[0].payee_name) >= SIM_AUTO:
            # Fecha corrida (operación vs liquidación) pero payee casi idéntico:
            # misma transacción, auto-adoptable sin humano.
            m = near[0]
            claimed.add(m.id)
            report.tier2_adoptable += 1
            report.details.append(_detail(t, 2, m))
            if apply:
                t.match_candidate_id = m.id
                t.match_tier = 2
        elif near:
            m = near[0]
            claimed.add(m.id)   # reservado para ESTA fila hasta que el humano decida
            report.tier3_review += 1
            report.details.append(_detail(t, 3, m))
            if apply:
                t.match_candidate_id = m.id
                t.match_tier = 3
                t.status = TxnStatus.needs_review
        else:
            report.tier4_new += 1
            if apply:
                t.match_tier = 4

    if apply:
        session.commit()
    return report


def _detail(t: Transaction, tier: int, m: ActualMirror) -> dict:
    return {
        "tier": tier,
        "txn_id": t.id,
        "date": t.date.isoformat(),
        "amount": str(t.amount),
        "payee": t.raw_payee[:40],
        "mirror_id": m.id,
        "mirror_date": m.date.isoformat(),
        "mirror_payee": (m.payee_name or "")[:40],
        "mirror_category": m.category_name,
        "mirror_imported_id": m.imported_id,
    }


# ── Adopción ─────────────────────────────────────────────────────────────────
def adopt(session: Session, txn: Transaction) -> ActualMirror:
    """Adopta el candidato de una fila: queda synced sin push. La categoría legacy
    gana (no se toca en Actual); la de spend-pipe queda como sugerencia visible.
    El backfill del imported_id a Actual lo hace claim_in_actual() en batch aparte.
    """
    if not txn.match_candidate_id:
        raise ValueError(f"{txn.id} no tiene candidato de reconciliación")
    mirror = session.get(ActualMirror, txn.match_candidate_id)
    txn.actual_txn_id = mirror.actual_txn_id
    txn.sync_origin = "adopted"
    txn.status = TxnStatus.synced
    return mirror


def reject_match(session: Session, txn: Transaction) -> None:
    """Rechaza el candidato: la fila queda como nueva de verdad (nivel 4)."""
    txn.match_candidate_id = None
    txn.match_tier = 4
    if txn.status == TxnStatus.needs_review and txn.category is not None \
            and not txn.pending and (txn.payee or "").strip():
        txn.status = TxnStatus.normalized


# NOTA: no existe backfill de imported_id hacia Actual — verificado 2026-07-03 que
# la API lo ignora en updateTransaction (también como financial_id) y el merge de
# importTransactions solo reclama txns SIN id. El claim durable es `already_linked`
# (actual_txn_id en staging) dentro de reconcile_import().
