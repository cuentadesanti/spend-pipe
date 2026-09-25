"""Revolut — extracto CSV de la app (cabeceras en español o en inglés).

Solo entran filas completadas: las pendientes o revertidas no afectan al saldo.
El importe neto es Importe − Comisión, y el candado es la cadena de saldos:
cada Saldo debe ser el anterior más el importe neto. Si no cuadra, se falla fuerte.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation

from ..schema import CommonTransaction
from ..statement import ParsedFile, StatementMeta
from .errors import LayoutNotRecognizedError, ParseValidationError

COLUMNS: dict[str, dict[str, str]] = {
    "es": {
        "type": "Tipo", "completed": "Fecha de finalización", "description": "Descripción",
        "amount": "Importe", "fee": "Comisión", "currency": "Divisa", "state": "Estado", "balance": "Saldo",
    },
    "en": {
        "type": "Type", "completed": "Completed Date", "description": "Description",
        "amount": "Amount", "fee": "Fee", "currency": "Currency", "state": "State", "balance": "Balance",
    },
}
COMPLETED_STATES = {"COMPLETADO", "COMPLETED"}


def revolut_columns(headers) -> dict[str, str] | None:
    present = {(h or "").strip() for h in headers}
    for cols in COLUMNS.values():
        if set(cols.values()) <= present:
            return cols
    return None


def _decimal(value: str | None, field: str, line: int) -> Decimal:
    try:
        return Decimal((value or "").strip() or "0")
    except InvalidOperation:
        raise ParseValidationError(f"Revolut: {field} inválido en la línea {line}: {value!r}") from None


class RevolutCsvParser:
    source_bank = "revolut"
    format = "csv"
    source_account = "Revolut"  # la cuenta real es 'Revolut <divisa>' y se fija por fila

    def parse(self, file_path: str) -> list[CommonTransaction]:
        return self.parse_statement(file_path).transactions

    def parse_statement(self, file_path: str) -> ParsedFile:
        with open(file_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cols = revolut_columns(reader.fieldnames or [])
            if cols is None:
                raise LayoutNotRecognizedError("CSV de Revolut no reconocido: faltan columnas esperadas.")
            rows = [
                (i + 2, row)
                for i, row in enumerate(reader)
                if (row.get(cols["state"]) or "").strip().upper() in COMPLETED_STATES
            ]

        currencies = {row[cols["currency"]].strip() for _, row in rows}
        if len(currencies) > 1:
            raise ParseValidationError(f"Revolut: el CSV mezcla divisas {sorted(currencies)}; exporta una cuenta por archivo.")

        rows.sort(key=lambda item: item[1][cols["completed"]])  # sort estable: respeta el orden del archivo en empates
        txns: list[CommonTransaction] = []
        opening: Decimal | None = None
        previous: Decimal | None = None
        for index, (line, row) in enumerate(rows):
            net = _decimal(row[cols["amount"]], "importe", line) - _decimal(row[cols["fee"]], "comisión", line)
            balance = _decimal(row[cols["balance"]], "saldo", line)
            if previous is None:
                opening = balance - net
            elif previous + net != balance:
                raise ParseValidationError(
                    f"Revolut: la cadena de saldos no cuadra en la línea {line}: {previous} + {net} ≠ {balance}"
                )
            previous = balance
            currency = row[cols["currency"]].strip()
            txns.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=f"Revolut {currency}",
                    format=self.format,
                    date=date.fromisoformat(row[cols["completed"]].strip()[:10]),
                    amount=net,
                    currency=currency,
                    raw_payee=row[cols["description"]].strip(),
                    notes=(row.get(cols["type"]) or "").strip() or None,
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=index,
                )
            )

        if not txns:
            return ParsedFile([])
        dates = [t.date for t in txns]
        return ParsedFile(
            txns,
            StatementMeta(period_from=min(dates), period_to=max(dates), opening_balance=opening, ending_balance=previous),
        )
