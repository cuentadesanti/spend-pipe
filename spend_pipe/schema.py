"""Schema común (Pydantic) y contrato del artefacto de batch aprobado.

`SCHEMA_VERSION` viaja en el artefacto desde el día uno: cuando el contrato cambie,
el worker Node puede decidir qué hacer en vez de romper en silencio.
"""
from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class CommonTransaction(BaseModel):
    """Salida de cualquier parser. Todo aguas abajo (staging, dedup, review, push)
    solo conoce esta forma — nunca el formato original (csv/xlsx/pdf/manual)."""

    source_bank: str
    source_account: str            # nombre de cuenta tal como lo trae la fuente (ej 'bbva-tdc')
    format: str                    # 'csv' | 'xlsx' | 'pdf' | 'manual'
    date: Date
    amount: Decimal                # decimal con signo; la conversión a centavos vive en el worker Node
    currency: str
    raw_payee: str                 # tal cual lo mandó el banco; base de la identidad
    notes: Optional[str] = None
    pending: bool = False          # autorizado/no liquidado → puede cambiar de importe o desaparecer
    source_file: Optional[str] = None
    source_row: Optional[int] = None


class BatchTransactionMeta(BaseModel):
    source_file: Optional[str] = None
    source_row: Optional[int] = None
    raw_payee: Optional[str] = None


class BatchTransaction(BaseModel):
    """Una transacción dentro del artefacto exportado hacia el worker Node."""

    spend_pipe_transaction_id: str
    date: Date
    amount: Decimal
    currency: str
    payee_name: str
    notes: Optional[str] = None
    imported_id: str
    cleared: bool = True
    category_name: Optional[str] = None   # null en MVP1; se puebla en MVP3 sin cambiar el contrato
    metadata: BatchTransactionMeta = Field(default_factory=BatchTransactionMeta)


class BatchAccountGroup(BaseModel):
    """Agrupado por cuenta porque `importTransactions` opera por cuenta.
    El worker Node solo mapea `actual_account_name` → account_id y hace el import por grupo."""

    actual_account_name: str
    source_account_name: str
    transactions: list[BatchTransaction]


class BatchArtifact(BaseModel):
    """El artefacto que cruza la frontera Python → Node: un batch aprobado, listo para push."""

    schema_version: str = SCHEMA_VERSION
    batch_id: str
    source: str = "spend-pipe"
    approved_at: datetime
    approved_by: str
    currency_default: str = "MXN"
    accounts: list[BatchAccountGroup]
