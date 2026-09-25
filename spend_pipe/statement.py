"""Metadatos de un extracto, además de sus movimientos.

`Parser.parse()` sigue devolviendo solo transacciones: staging, review y push no
cambian. Los parsers que saben leer el periodo y los saldos del documento
implementan además `parse_statement()`; el resto obtiene un periodo a partir de
las fechas de sus movimientos y ningún saldo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .schema import CommonTransaction


@dataclass(frozen=True)
class StatementMeta:
    period_from: date | None = None
    period_to: date | None = None
    opening_balance: Decimal | None = None
    ending_balance: Decimal | None = None


@dataclass(frozen=True)
class ParsedFile:
    transactions: list[CommonTransaction]
    meta: StatementMeta = field(default_factory=StatementMeta)

    def period(self) -> tuple[date, date]:
        """Periodo del documento si el parser lo leyó; si no, el rango de fechas de los movimientos."""
        dates = [t.date for t in self.transactions]
        start = self.meta.period_from or (min(dates) if dates else None)
        end = self.meta.period_to or (max(dates) if dates else None)
        if start is None or end is None:
            raise ValueError("El extracto no tiene movimientos ni periodo")
        return start, end


def parse_statement(parser, file_path: str) -> ParsedFile:
    method = getattr(parser, "parse_statement", None)
    if callable(method):
        return method(file_path)
    return ParsedFile(parser.parse(file_path))
