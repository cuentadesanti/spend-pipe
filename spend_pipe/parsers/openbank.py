"""Parser de 'Movimientos de Tarjeta' de Openbank.

El archivo es un '.xls' que en realidad es HTML. Columnas (posicionales, específicas
de Openbank): Fecha Operación · Hora · Concepto · Situación · Localidad · Importe · Divisa.
Importes en formato europeo (coma decimal, punto de miles): '-1.163,33'.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from ..schema import CommonTransaction
from .html_table import extract_rows

_DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
# Openbank intercala celdas espaciadoras vacías: los datos reales viven en índices
# impares (1,3,5,7,9,11) y la divisa en 12. Mapear por índice fijo (no colapsar vacíos)
# es robusto ante campos reales ausentes, ej. localidad vacía en compras online.
_C_FECHA, _C_HORA, _C_CONCEPTO, _C_SITUACION, _C_LOCALIDAD, _C_IMPORTE, _C_DIVISA = 1, 3, 5, 7, 9, 11, 12


def parse_eu_amount(raw: str) -> Decimal:
    """'-1.163,33' → Decimal('-1163.33'). Maneja punto de miles y coma decimal."""
    s = (raw or "").strip().replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")   # '.' es miles, ',' es decimal
    return Decimal(s)


class OpenbankCardParser:
    format = "xls"

    def __init__(self, source_bank: str, source_account: str, currency: str = "EUR"):
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency

    def parse(self, file_path: str) -> list[CommonTransaction]:
        html = open(file_path, encoding="utf-8", errors="replace").read()
        rows = extract_rows(html)

        out: list[CommonTransaction] = []
        for i, row in enumerate(rows):
            # Fila de datos: tiene la celda de divisa y una fecha DD-MM-YYYY en su columna.
            if len(row) <= _C_DIVISA or not _DATE.match(row[_C_FECHA]):
                continue
            localidad = row[_C_LOCALIDAD].strip()
            situacion = row[_C_SITUACION].strip()
            # Solo LIQUIDADO es definitivo; AUTORIZADO (u otro) es una autorización que
            # puede re-liquidarse con otro importe o desaparecer → pending.
            pending = situacion.upper() != "LIQUIDADO"
            out.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=self.source_account,
                    format=self.format,
                    date=datetime.strptime(row[_C_FECHA], "%d-%m-%Y").date(),
                    amount=parse_eu_amount(row[_C_IMPORTE]),
                    currency=row[_C_DIVISA].strip() or self.currency,
                    raw_payee=row[_C_CONCEPTO].strip(),
                    notes=" · ".join(x for x in (localidad, situacion) if x) or None,
                    pending=pending,
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=i,
                )
            )
        return out
