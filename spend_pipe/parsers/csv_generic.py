"""Parser CSV genérico, configurable por un mapeo de columnas.

Cada banco que exporta CSV se vuelve una instancia de `GenericCsvParser` con su
propio `CsvColumnMap`. El parseo en sí (montos, fechas, encoding) es común.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ..schema import CommonTransaction


@dataclass(frozen=True)
class CsvColumnMap:
    """Qué columnas del CSV mapean a los campos del schema común."""

    date: str
    amount: str
    payee: str
    memo: str | None = None
    date_format: str | None = None   # None = ISO (YYYY-MM-DD)


def _parse_amount(raw: str) -> Decimal:
    """Monto con signo a Decimal, tolerando espacios y separadores de miles."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("monto vacío")
    try:
        return Decimal(s)
    except InvalidOperation:
        # Reintento quitando separadores de miles estilo en-US (1,234.56).
        return Decimal(s.replace(",", ""))


def _parse_date(raw: str, fmt: str | None) -> "datetime.date":
    s = (raw or "").strip()
    if fmt:
        return datetime.strptime(s, fmt).date()
    return datetime.strptime(s, "%Y-%m-%d").date()


class GenericCsvParser:
    format = "csv"

    def __init__(
        self,
        source_bank: str,
        source_account: str,
        currency: str,
        colmap: CsvColumnMap,
        encoding: str = "utf-8-sig",
    ):
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency
        self.colmap = colmap
        self.encoding = encoding

    def parse(self, file_path: str) -> list[CommonTransaction]:
        out: list[CommonTransaction] = []
        with open(file_path, newline="", encoding=self.encoding) as f:
            reader = csv.DictReader(f)
            # La fila 1 es el header; los datos empiezan en la 2.
            for i, row in enumerate(reader, start=2):
                cm = self.colmap
                memo = (row.get(cm.memo) or "").strip() if cm.memo else None
                out.append(
                    CommonTransaction(
                        source_bank=self.source_bank,
                        source_account=self.source_account,
                        format=self.format,
                        date=_parse_date(row[cm.date], cm.date_format),
                        amount=_parse_amount(row[cm.amount]),
                        currency=self.currency,
                        raw_payee=(row.get(cm.payee) or "").strip(),
                        notes=memo or None,
                        source_file=file_path.rsplit("/", 1)[-1],
                        source_row=i,
                    )
                )
        return out
