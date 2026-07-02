"""Parser 'manual': puente para fuentes sin parser todavía (ej. PDF de BBVA hasta MVP5).

En el Web UI pegas las filas del estado de cuenta y entran a staging como cualquier
otra fuente, con format='manual'. Cuando llegue el parser real, solo cambia el origen
de las filas; el resto del pipeline no se entera.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal

from ..schema import CommonTransaction

# Columnas esperadas en el texto pegado (CSV o TSV con header).
_REQUIRED = {"date", "amount", "payee"}


class ManualParser:
    format = "manual"

    def __init__(self, source_bank: str, source_account: str, currency: str):
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency

    def parse_text(self, pasted: str) -> list[CommonTransaction]:
        """Parsea texto pegado con header: columnas date, amount, payee[, memo]."""
        sample = pasted.strip().splitlines()
        if not sample:
            return []
        # Detecta coma o tab como separador.
        delimiter = "\t" if "\t" in sample[0] else ","
        reader = csv.DictReader(io.StringIO(pasted), delimiter=delimiter)
        cols = {c.strip().lower() for c in (reader.fieldnames or [])}
        missing = _REQUIRED - cols
        if missing:
            raise ValueError(f"Faltan columnas en el texto pegado: {', '.join(sorted(missing))}")

        out: list[CommonTransaction] = []
        for i, row in enumerate(reader, start=2):
            norm = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            out.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=self.source_account,
                    format=self.format,
                    date=datetime.strptime(norm["date"], "%Y-%m-%d").date(),
                    amount=Decimal(norm["amount"].replace(",", "")),
                    currency=self.currency,
                    raw_payee=norm["payee"],
                    notes=norm.get("memo") or None,
                    source_file="manual-paste",
                    source_row=i,
                )
            )
        return out

    # Compat con la interfaz Parser (parse desde archivo de texto).
    def parse(self, file_path: str) -> list[CommonTransaction]:
        with open(file_path, encoding="utf-8") as f:
            return self.parse_text(f.read())
