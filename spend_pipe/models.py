"""Modelos de staging (SQLAlchemy 2.0). Filas planas: staging quiere queries y
constraints; el artefacto exportado es el que agrupa por cuenta.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date as Date
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .schema import SCHEMA_VERSION


def _enum(e):
    """Columna Enum que guarda el .value (ej. 'approved') y devuelve el miembro del enum."""
    return SqlEnum(e, values_callable=lambda enum: [m.value for m in enum])


class Base(DeclarativeBase):
    pass


def _uuid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TxnStatus(str, enum.Enum):
    """Ciclo de vida de una transacción. Ortogonal a `is_duplicate` y `confidence`,
    que NO son estados (una txn puede ser `normalized` y a la vez duplicada)."""

    parsed = "parsed"              # extraída a staging desde una fuente
    normalized = "normalized"      # pasó las reglas con confianza suficiente → "se ve bien"
    needs_review = "needs_review"  # sin match / confianza baja / sugerencia de LLM → resaltada
    approved = "approved"          # aprobada por humano dentro de un batch
    synced = "synced"              # empujada a Actual (tiene actual_txn_id)
    rejected = "rejected"          # descartada por humano (terminal, no se pushea)
    paired = "paired"              # pata + de una transferencia: la representa la
                                   # contraparte que Actual auto-crea; nunca se pushea


class ImportStatus(str, enum.Enum):
    received = "received"
    parsed = "parsed"
    failed = "failed"


class BatchStatus(str, enum.Enum):
    open = "open"
    approved = "approved"
    pushed = "pushed"


class AppSetting(Base):
    """Ajustes de la app editables desde la UI (ej. la API key de Anthropic).

    Viven en la base (no en .env) para sobrevivir redeploys con filesystem
    efímero (Railway). Los valores sensibles se muestran enmascarados en la UI.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class CsvMapping(Base):
    """Mapeo de columnas 'aprendido' para un layout CSV: la firma del header lo
    reconoce en futuros uploads y el archivo entra solo, sin re-mapear."""

    __tablename__ = "csv_mappings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _uuid("map"))
    name: Mapped[str] = mapped_column(String)                      # ej. 'BBVA MX app — cuenta'
    header_signature: Mapped[str] = mapped_column(String, index=True)
    source_bank: Mapped[str] = mapped_column(String)
    source_account: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String, default="MXN")
    delimiter: Mapped[str] = mapped_column(String, default=",")
    colmap_json: Mapped[str] = mapped_column(Text)                 # CsvColumnMap serializado
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ActualMirror(Base):
    """Espejo local de las transacciones que YA existen en Actual (cualquier origen:
    imports legacy de finanzas-ai, entradas manuales, pushes de spend-pipe).

    Es lo que le da visibilidad al matcher de reconciliación sobre data histórica que
    spend-pipe no ingirió — sin esto, el dedup solo ve su propio staging y un push de
    un archivo con historia puede doble-contar contra imports legacy (caso real:
    'Movimientos de Cuenta.xls' con 483 txns de un año, ids formato finanzas-ai).
    Se refresca vía node-pusher/get_transactions.js (solo lectura).
    """

    __tablename__ = "actual_mirror"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _uuid("mir"))
    actual_txn_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    account_name: Mapped[str] = mapped_column(String, index=True)   # nombre en Actual (destino)
    date: Mapped[Date] = mapped_column(index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, index=True)  # Actual guarda centavos
    payee_name: Mapped[str | None] = mapped_column(String, nullable=True)
    category_name: Mapped[str | None] = mapped_column(String, nullable=True)
    imported_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_parent: Mapped[bool] = mapped_column(Boolean, default=False)     # padre de splits
    transfer_id: Mapped[str | None] = mapped_column(String, nullable=True)  # pata vinculada
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# Búsqueda del matcher: cuenta + monto (+ fecha por rango).
Index("ix_mirror_match", ActualMirror.account_name, ActualMirror.amount_cents, ActualMirror.date)


class Import(Base):
    """Un archivo cargado. Entidad de primera clase: permite auditar y hacer rollback."""

    __tablename__ = "imports"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _uuid("imp"))
    source_bank: Mapped[str] = mapped_column(String)
    source_file: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String, index=True)  # detecta recarga del mismo archivo
    format: Mapped[str] = mapped_column(String)                 # csv | xlsx | pdf | manual
    raw_file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[ImportStatus] = mapped_column(_enum(ImportStatus), default=ImportStatus.received)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="import_", cascade="all, delete-orphan")


class Batch(Base):
    """Un conjunto de transacciones aprobadas juntas para push."""

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _uuid("batch"))
    schema_version: Mapped[str] = mapped_column(String, default=SCHEMA_VERSION)
    status: Mapped[BatchStatus] = mapped_column(_enum(BatchStatus), default=BatchStatus.open)
    approved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="batch")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _uuid("txn"))
    import_id: Mapped[str] = mapped_column(ForeignKey("imports.id"))
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("batches.id"), nullable=True)

    # --- procedencia ---
    source_bank: Mapped[str] = mapped_column(String)
    source_account: Mapped[str] = mapped_column(String)
    format: Mapped[str] = mapped_column(String)
    source_file: Mapped[str | None] = mapped_column(String, nullable=True)
    source_row: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- datos de la transacción ---
    date: Mapped[Date] = mapped_column(default=None)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String)
    raw_payee: Mapped[str] = mapped_column(String)          # crudo; base de imported_id
    payee: Mapped[str | None] = mapped_column(String, nullable=True)   # normalizado; se muestra
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)  # null hasta MVP3

    # --- tipo de movimiento (pasada 2; stub en MVP1) ---
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_pair_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Autorizado/no liquidado (ej. Openbank AUTORIZADO): puede cambiar o desaparecer,
    # así que no debe llegar a Actual sin revisión → fuerza needs_review.
    pending: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- identidad ---
    imported_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    dedup_hash: Mapped[str | None] = mapped_column(String, index=True, nullable=True)  # se llena en MVP2

    # --- clasificación / estado (ortogonales entre sí) ---
    status: Mapped[TxnStatus] = mapped_column(_enum(TxnStatus), default=TxnStatus.parsed, index=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[str | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)

    # --- resultado del push / reconciliación ---
    actual_txn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Cómo llegó a synced: 'push' (la empujamos) | 'adopted' (ya existía en Actual,
    # p.ej. de un import legacy, y la reclamamos en vez de re-insertarla).
    sync_origin: Mapped[str | None] = mapped_column(String, nullable=True)
    # Match dudoso de reconciliación (nivel 3): candidato en el espejo, pendiente de
    # que un humano acepte (→ adopción) o rechace (→ queda como nueva).
    match_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("actual_mirror.id"), nullable=True)
    match_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1..4, ver reconcile.py

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    import_: Mapped["Import"] = relationship(back_populates="transactions")
    batch: Mapped["Batch"] = relationship(back_populates="transactions")
    splits: Mapped[list["TransactionSplit"]] = relationship(
        back_populates="transaction",
        cascade="all, delete-orphan",
        order_by="TransactionSplit.position",
    )


class TransactionSplit(Base):
    __tablename__ = "transaction_splits"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: _uuid("split"))
    txn_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    category: Mapped[str] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    transaction: Mapped["Transaction"] = relationship(back_populates="splits")


# Búsqueda típica del review: "¿qué quedó pendiente en este batch?"
Index("ix_txn_batch_status", Transaction.batch_id, Transaction.status)
