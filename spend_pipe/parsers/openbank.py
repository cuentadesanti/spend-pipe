"""Parsers de los '.xls' de Openbank (HTML disfrazado): tarjeta y cuenta.

Tarjeta ('Movimientos de Tarjeta'): Fecha Operación · Hora · Concepto · Situación ·
Localidad · Importe · Divisa, fechas DD-MM-YYYY.
Cuenta ('Cuentas - Movimientos'): Fecha Operación · Fecha Valor · Concepto · Importe ·
Saldo, fechas DD/MM/YYYY. El saldo corrido permite validar la extracción completa.
Importes en formato europeo (coma decimal, punto de miles): '-1.163,33'.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from ..schema import CommonTransaction
from .errors import ParseValidationError
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


# 'Cuentas - Movimientos': 10 celdas por fila con espaciadoras vacías intercaladas.
_DATE_SLASH = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_A_FECHA_OP, _A_FECHA_VALOR, _A_CONCEPTO, _A_IMPORTE, _A_SALDO = 1, 3, 5, 7, 9


class OpenbankAccountParser:
    """Extracto de la cuenta corriente. Incluye los cargos de la tarjeta de débito
    (con fecha de liquidación, no de operación) más todo lo que no pasa por tarjeta:
    nóminas, Bizum, transferencias, comisiones. La convergencia con lo ya existente
    en Actual la resuelve la reconciliación, no este parser."""

    format = "xls"

    def __init__(self, source_bank: str, source_account: str, currency: str = "EUR"):
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency

    def parse(self, file_path: str) -> list[CommonTransaction]:
        raw = open(file_path, "rb").read()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("latin-1")
        rows = extract_rows(html)

        header_saldo: Decimal | None = None
        data: list[tuple[int, list[str]]] = []
        for i, row in enumerate(rows):
            if len(row) >= 4 and row[1].strip() == "Saldo:":
                header_saldo = parse_eu_amount(row[3].replace("EUR", ""))
            if len(row) > _A_SALDO and _DATE_SLASH.match(row[_A_FECHA_OP].strip()):
                data.append((i, row))

        # Candado: el saldo corrido debe encadenar fila a fila (más reciente primero),
        # y el saldo del encabezado debe ser el de la primera fila. Si algo no cuadra,
        # la extracción perdió o deformó movimientos → fallar fuerte.
        for j in range(len(data) - 1):
            saldo = parse_eu_amount(data[j][1][_A_SALDO])
            saldo_prev = parse_eu_amount(data[j + 1][1][_A_SALDO])
            importe = parse_eu_amount(data[j][1][_A_IMPORTE])
            if saldo != saldo_prev + importe:
                raise ParseValidationError(
                    f"Saldo corrido no encadena en la fila {data[j][0]} "
                    f"({data[j][1][_A_FECHA_OP]}): {saldo_prev} + {importe} != {saldo}"
                )
        if header_saldo is not None and data and header_saldo != parse_eu_amount(data[0][1][_A_SALDO]):
            raise ParseValidationError(
                f"El saldo del encabezado ({header_saldo}) no coincide con el de la "
                f"primera fila ({parse_eu_amount(data[0][1][_A_SALDO])})"
            )

        out: list[CommonTransaction] = []
        for i, row in data:
            fecha_valor = row[_A_FECHA_VALOR].strip()
            fecha_op = row[_A_FECHA_OP].strip()
            out.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=self.source_account,
                    format=self.format,
                    date=datetime.strptime(fecha_op, "%d/%m/%Y").date(),
                    amount=parse_eu_amount(row[_A_IMPORTE]),
                    currency=self.currency,
                    raw_payee=row[_A_CONCEPTO].strip(),
                    notes=f"valor {fecha_valor}" if fecha_valor and fecha_valor != fecha_op else None,
                    pending=False,   # el extracto solo trae movimientos liquidados
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=i,
                )
            )
        return out
