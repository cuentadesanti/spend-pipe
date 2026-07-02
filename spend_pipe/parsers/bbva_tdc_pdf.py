"""Parser determinista del estado de cuenta PDF de la tarjeta de crédito BBVA (TDC).

Diferencias con el de Cuenta Digital (bbva_pdf.py):
- El monto lleva SIGNO EXPLÍCITO en el texto (`+ $532.00` / `- $25,241.80`), pero con
  semántica de tarjeta: `+` = cargo/compra (gasto → negativo en nuestro schema) y
  `-` = abono/pago (positivo). Se invierte SIEMPRE: schema_amount = -monto_del_estado.
- Fechas completas con año (`05-may-2026`), mes abreviado en español en minúsculas.
- Filas: fecha operación + fecha de cargo + descripción + monto; las líneas de
  continuación (IVA/USD/TIPO DE CAMBIO) van a notes.
- Candado: reconciliación contra `TOTAL CARGOS` y `TOTAL ABONOS` del desglose.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

import pdfplumber

from ..schema import CommonTransaction
from .errors import LayoutNotRecognizedError, ParseValidationError

_ES_MONTHS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
              "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}
_FULLDATE = re.compile(r"^(\d{2})-([a-z]{3})-(\d{4})$")
_AMOUNT = re.compile(r"^[+-]?\$?[\d,]+\.\d{2}$")
_TOTAL_CARGOS = re.compile(r"TOTAL CARGOS\s+\$?([\d,]+\.\d{2})")
_TOTAL_ABONOS = re.compile(r"TOTAL ABONOS\s+-?\$?([\d,]+\.\d{2})")

# Al ver cualquiera de estos, se corta la absorción de líneas de continuación
# (headers repetidos por página, footers, totales).
_TERMINATORS = (
    "TOTAL CARGOS", "TOTAL ABONOS", "Notas:", "Número de cuenta", "Página",
    "CARGOS,COMPRAS", "DESGLOSE", "Descripción del movimiento", "ATENCIÓN",
)


def _parse_full_date(token: str) -> date | None:
    m = _FULLDATE.match(token)
    if not m:
        return None
    month = _ES_MONTHS.get(m.group(2))
    if month is None:
        return None
    return date(int(m.group(3)), month, int(m.group(1)))


def _money(s: str) -> Decimal:
    return Decimal(s.replace("$", "").replace(",", "").replace("+", "").strip())


class BbvaTdcPdfParser:
    format = "pdf"

    def __init__(self, source_bank: str, source_account: str, currency: str = "MXN"):
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency

    def parse(self, file_path: str) -> list[CommonTransaction]:
        lines: list[list[str]] = []          # cada línea = lista de textos de word
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                by_top: dict[int, list] = defaultdict(list)
                for w in page.extract_words():
                    by_top[round(w["top"])].append(w)
                for top in sorted(by_top):
                    lines.append([w["text"] for w in sorted(by_top[top], key=lambda x: x["x0"])])

        full_text = " ".join(" ".join(ws) for ws in lines)
        if "DESGLOSE DE MOVIMIENTOS" not in full_text:
            raise LayoutNotRecognizedError(
                "BBVA TDC PDF no reconocido: no se encontró 'DESGLOSE DE MOVIMIENTOS'."
            )
        mt_c = _TOTAL_CARGOS.search(full_text)
        mt_a = _TOTAL_ABONOS.search(full_text)
        if not mt_c or not mt_a:
            raise LayoutNotRecognizedError(
                "BBVA TDC PDF: no se encontraron TOTAL CARGOS / TOTAL ABONOS para reconciliar."
            )
        total_cargos = _money(mt_c.group(1))
        total_abonos = _money(mt_a.group(1))

        txns: list[CommonTransaction] = []
        cur: CommonTransaction | None = None
        stmt_cargos = Decimal("0")   # suma de '+' del estado (gastos)
        stmt_abonos = Decimal("0")   # suma de '-' del estado (pagos)

        for i, ws in enumerate(lines):
            text = " ".join(ws)
            if any(t in text for t in _TERMINATORS):
                cur = None
                continue

            d_oper = _parse_full_date(ws[0]) if ws else None
            d_cargo = _parse_full_date(ws[1]) if len(ws) > 1 else None
            if d_oper and d_cargo:
                # Fila de movimiento: [fecha_oper, fecha_cargo, desc..., (±) monto]
                tail = ws[2:]
                sign = 1
                if tail and _AMOUNT.match(tail[-1]):
                    raw_amt = tail[-1]
                    tail = tail[:-1]
                    if tail and tail[-1] in ("+", "-"):
                        sign = -1 if tail[-1] == "-" else 1
                        tail = tail[:-1]
                    elif raw_amt.startswith("-"):
                        sign = -1
                    stmt_value = _money(raw_amt) * sign      # signo del ESTADO
                else:
                    raise ParseValidationError(f"Fila de movimiento sin monto: {text!r}")

                if stmt_value > 0:
                    stmt_cargos += stmt_value
                else:
                    stmt_abonos += -stmt_value

                cur = CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=self.source_account,
                    format=self.format,
                    date=d_oper,                              # fecha de operación
                    amount=-stmt_value,                       # semántica tarjeta: se invierte
                    currency=self.currency,
                    raw_payee=" ".join(tail).strip(),
                    notes=None,
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=i,
                )
                txns.append(cur)
            elif cur is not None and "$" in text:
                # Continuación (IVA/USD/TIPO DE CAMBIO…) → notes del movimiento actual.
                cur.notes = f"{cur.notes}; {text}" if cur.notes else text

        # ── Candado de reconciliación ──
        problems = []
        if stmt_cargos != total_cargos:
            problems.append(f"suma cargos {stmt_cargos} ≠ TOTAL CARGOS {total_cargos}")
        if stmt_abonos != total_abonos:
            problems.append(f"suma abonos {stmt_abonos} ≠ TOTAL ABONOS {total_abonos}")
        if problems:
            raise ParseValidationError("BBVA TDC PDF no reconcilia: " + "; ".join(problems))
        return txns
