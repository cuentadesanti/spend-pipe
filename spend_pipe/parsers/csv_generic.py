"""Parser CSV genérico, configurable por un mapeo de columnas.

Cada banco que exporta CSV se vuelve una instancia de `GenericCsvParser` con su
propio `CsvColumnMap` — ya sea registrada en código (configured.py) o construida
al vuelo desde el form de mapeo del Web UI.

Maneja los casos reales de exports bancarios:
- Preámbulo antes del header (metadata de la cuenta): se escanea la fila de header.
- Importes con '$', espacios, separador de miles, y placeholder '-' para vacío.
- Columna única de importe O columnas separadas debit/credit (RETIRO/DEPOSITO).
- Fechas ISO, DD/MM/YYYY o con mes abreviado en español (20/Jun/2026).
- Exports de tarjeta con cargos en positivo: `invert_sign` los da vuelta.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..schema import CommonTransaction

# Meses abreviados en español → inglés (para strptime %b, que es locale-dependiente).
_ES_MONTHS = {"ene": "Jan", "feb": "Feb", "mar": "Mar", "abr": "Apr", "may": "May",
              "jun": "Jun", "jul": "Jul", "ago": "Aug", "sep": "Sep", "oct": "Oct",
              "nov": "Nov", "dic": "Dec"}
_MONTH_TOKEN = re.compile(r"(?<=/)([A-Za-z]{3})(?=/)")
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%b/%Y", "%m/%d/%Y")
_EMPTY = {"", "-", "--", "N/A", "n/a"}


@dataclass(frozen=True)
class CsvColumnMap:
    """Qué columnas del CSV mapean a los campos del schema común.

    Importe: o `amount` (columna única con signo) o `debit`+`credit` (separadas).
    """

    date: str
    payee: str
    amount: str | None = None
    debit: str | None = None       # retiros/cargos; se fuerza negativo
    credit: str | None = None      # depósitos/abonos; se fuerza positivo
    memo: str | None = None
    date_format: str | None = None  # None = auto (prueba formatos comunes)
    invert_sign: bool = False       # exports de tarjeta con gastos en positivo


def parse_amount(raw: str | None) -> Decimal | None:
    """'$ 1,029.25' → Decimal('1029.25'). '-' / '' → None. Conserva el signo."""
    s = (raw or "").strip()
    if s in _EMPTY:
        return None
    s = s.replace("$", "").replace(" ", "")
    if s in _EMPTY:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        # Separador de miles estilo en-US (1,234.56).
        try:
            return Decimal(s.replace(",", ""))
        except InvalidOperation as e:
            raise ValueError(f"importe no parseable: {raw!r}") from e


def parse_date_flex(raw: str, fmt: str | None) -> date:
    """Fecha con formato fijo o autodetección, normalizando meses en español."""
    s = (raw or "").strip()
    s = _MONTH_TOKEN.sub(lambda m: _ES_MONTHS.get(m.group(1).lower(), m.group(1)), s)
    if fmt:
        return datetime.strptime(s, fmt).date()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(f"fecha no parseable: {raw!r}")


def find_header_row(lines: list[str], required: list[str], delimiter: str = ",") -> int:
    """Índice (0-based) de la línea que contiene todas las columnas requeridas.

    Los exports bancarios suelen traer preámbulo (metadata de cuenta) antes del header.
    """
    req = {r.strip().lower() for r in required}
    for i, line in enumerate(lines[:30]):
        cells = {c.strip().lower() for c in line.split(delimiter)}
        if req <= cells:
            return i
    raise ValueError(f"No se encontró la fila de header con las columnas {sorted(req)}")


class GenericCsvParser:
    format = "csv"

    def __init__(
        self,
        source_bank: str,
        source_account: str,
        currency: str,
        colmap: CsvColumnMap,
        encoding: str = "utf-8-sig",
        delimiter: str = ",",
    ):
        if not colmap.amount and not (colmap.debit or colmap.credit):
            raise ValueError("CsvColumnMap necesita `amount` o `debit`/`credit`")
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency
        self.colmap = colmap
        self.encoding = encoding
        self.delimiter = delimiter

    def _row_amount(self, row: dict) -> Decimal | None:
        cm = self.colmap
        if cm.amount:
            value = parse_amount(row.get(cm.amount))
        else:
            debit = parse_amount(row.get(cm.debit)) if cm.debit else None
            credit = parse_amount(row.get(cm.credit)) if cm.credit else None
            if debit is not None:
                value = -abs(debit)      # sign-aware: '$ -2000' y '2000' dan lo mismo
            elif credit is not None:
                value = abs(credit)
            else:
                value = None
        if value is not None and cm.invert_sign:
            value = -value
        return value

    def parse(self, file_path: str) -> list[CommonTransaction]:
        with open(file_path, newline="", encoding=self.encoding) as f:
            raw_lines = f.read().splitlines()

        cm = self.colmap
        required = [c for c in (cm.date, cm.payee, cm.amount, cm.debit, cm.credit) if c]
        header_idx = find_header_row(raw_lines, required, self.delimiter)

        reader = csv.DictReader(
            io.StringIO("\n".join(raw_lines[header_idx:])), delimiter=self.delimiter
        )
        out: list[CommonTransaction] = []
        for i, row in enumerate(reader, start=header_idx + 2):  # fila real en el archivo
            row = {(k or "").strip(): v for k, v in row.items()}
            if not (row.get(cm.date) or "").strip():
                continue   # filas de relleno/totales al final
            amount = self._row_amount(row)
            if amount is None:
                continue
            memo = (row.get(cm.memo) or "").strip() if cm.memo else None
            out.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=self.source_account,
                    format=self.format,
                    date=parse_date_flex(row[cm.date], cm.date_format),
                    amount=amount,
                    currency=self.currency,
                    raw_payee=(row.get(cm.payee) or "").strip(),
                    notes=memo if memo not in _EMPTY else None,
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=i,
                )
            )
        return out
