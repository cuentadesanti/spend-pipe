"""Parser determinista del estado de cuenta PDF de BBVA (débito, ej. Cuenta Digital).

El tier difícil (MVP5). Retos que resuelve, todos por coordenadas reales (pdfplumber):
- Registros multi-línea: fecha/importe/descripción repartidos en 2+ líneas.
- Signo por COLUMNA: un número es cargo (−) o abono (+) según a qué columna se alinea
  (CARGOS/ABONOS), no por heurística de texto. Los saldos (OPER/LIQ) se ignoran.
- Fecha DD/MMM sin año: el año sale del 'Periodo DEL .. AL ..'. Meses en español.
- Formato MX: '201,917.81' (coma miles, punto decimal).

El candado: tras extraer, se RECONCILIA contra el resumen del estado (totales de
cargos/abonos, conteos, y saldo anterior + neto == saldo final). Si no cuadra, se
lanza ParseValidationError — nunca un parse silenciosamente incorrecto. Sin fallback LLM.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pdfplumber

from ..schema import CommonTransaction
from .errors import LayoutNotRecognizedError, ParseValidationError

_MONTHS = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12}
_DATE = re.compile(r"^\d{2}/[A-Z]{3}$")
_NUM = re.compile(r"^\d[\d,]*\.\d{2}$")
_PERIODO = re.compile(r"DEL\s+\d{2}/(\d{2})/(\d{4})\s+AL\s+\d{2}/(\d{2})/(\d{4})")
_RE_ABONOS = re.compile(r"Abonos\s*\(\+\)\s+(\d+)\s+([\d,]+\.\d{2})")
_RE_CARGOS = re.compile(r"Cargos\s*\(-\)\s+(\d+)\s+([\d,]+\.\d{2})")
_RE_SALDO_ANT = re.compile(r"Saldo Anterior\s+([\d,]+\.\d{2})")
_RE_SALDO_FIN = re.compile(r"Saldo Final\s+([\d,]+\.\d{2})")

# Líneas que cierran/no son movimientos (footers, headers de página, totales, resumen).
_TERMINATORS = (
    "Total de Movimientos", "La GAT", "BBVA MEXICO", "Le informamos", "Estado de Cuenta",
    "PAGINA", "No. de Cuenta", "No. de Cliente", "Libretón", "Detalle de Movimientos",
    "Información Financiera",
)


def parse_mx_amount(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


@dataclass
class StatementSummary:
    abonos_total: Decimal
    abonos_count: int
    cargos_total: Decimal      # magnitud positiva
    cargos_count: int
    saldo_anterior: Decimal
    saldo_final: Decimal


def validate_against_summary(txns: list[CommonTransaction], s: StatementSummary) -> None:
    """Candado de reconciliación. Lanza ParseValidationError con detalle si no cuadra."""
    abonos = [t for t in txns if t.amount > 0]
    cargos = [t for t in txns if t.amount < 0]
    abonos_sum = sum((t.amount for t in abonos), Decimal("0"))
    cargos_sum = sum((-t.amount for t in cargos), Decimal("0"))

    problems: list[str] = []
    if len(abonos) != s.abonos_count:
        problems.append(f"conteo abonos {len(abonos)} ≠ resumen {s.abonos_count}")
    if len(cargos) != s.cargos_count:
        problems.append(f"conteo cargos {len(cargos)} ≠ resumen {s.cargos_count}")
    if abonos_sum != s.abonos_total:
        problems.append(f"suma abonos {abonos_sum} ≠ resumen {s.abonos_total}")
    if cargos_sum != s.cargos_total:
        problems.append(f"suma cargos {cargos_sum} ≠ resumen {s.cargos_total}")
    esperado_final = s.saldo_anterior + abonos_sum - cargos_sum
    if esperado_final != s.saldo_final:
        problems.append(
            f"saldo anterior {s.saldo_anterior} + neto {abonos_sum - cargos_sum} "
            f"= {esperado_final} ≠ saldo final {s.saldo_final}"
        )
    if problems:
        raise ParseValidationError("BBVA-PDF no reconcilia con el resumen: " + "; ".join(problems))


class BbvaPdfParser:
    format = "pdf"

    def __init__(self, source_bank: str, source_account: str, currency: str = "MXN"):
        self.source_bank = source_bank
        self.source_account = source_account
        self.currency = currency

    def parse(self, file_path: str) -> list[CommonTransaction]:
        lines: list[list[dict]] = []
        header: dict[str, dict] | None = None
        period: tuple[int, int, int, int] | None = None
        full_text_parts: list[str] = []

        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                words = page.extract_words()
                page_text = " ".join(w["text"] for w in words)
                full_text_parts.append(page_text)
                if period is None:
                    m = _PERIODO.search(page_text)
                    if m:
                        period = (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))
                by_top: dict[int, list[dict]] = defaultdict(list)
                for w in words:
                    by_top[round(w["top"])].append(w)
                for top in sorted(by_top):
                    ws = sorted(by_top[top], key=lambda w: w["x0"])
                    lines.append(ws)
                    texts = {w["text"] for w in ws}
                    if header is None and "CARGOS" in texts and "ABONOS" in texts:
                        header = {w["text"]: w for w in ws}

        if header is None:
            raise LayoutNotRecognizedError(
                "BBVA-PDF no reconocido: no se encontró la fila de encabezados CARGOS/ABONOS."
            )
        if period is None:
            raise LayoutNotRecognizedError(
                "BBVA-PDF no reconocido: no se encontró el 'Periodo DEL .. AL ..' para inferir el año."
            )

        full_text = " ".join(full_text_parts)
        summary = self._parse_summary(full_text)

        anchors = {k: header[k]["x1"] for k in ("CARGOS", "ABONOS", "OPERACION", "LIQUIDACION")}
        ref_x0 = header["REFERENCIA"]["x0"]
        cargos_x0 = header["CARGOS"]["x0"]

        txns: list[CommonTransaction] = []
        for i, mov in enumerate(self._group_movements(lines)):
            oper = mov[0][0]["text"]
            raw_payee, notes = self._movement_text(mov, ref_x0, cargos_x0)
            txns.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=self.source_account,
                    format=self.format,
                    date=date(self._resolve_year(_MONTHS[oper.split("/")[1]], period),
                              _MONTHS[oper.split("/")[1]], int(oper.split("/")[0])),
                    amount=self._movement_amount(mov, anchors),
                    currency=self.currency,
                    raw_payee=raw_payee,
                    notes=notes,
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=i,
                )
            )

        validate_against_summary(txns, summary)   # el candado
        return txns

    @staticmethod
    def _parse_summary(full_text: str) -> StatementSummary:
        def need(rx, label):
            m = rx.search(full_text)
            if not m:
                raise LayoutNotRecognizedError(f"BBVA-PDF: no se encontró '{label}' en el resumen.")
            return m
        ab = need(_RE_ABONOS, "Depósitos / Abonos (+)")
        ca = need(_RE_CARGOS, "Retiros / Cargos (-)")
        return StatementSummary(
            abonos_total=parse_mx_amount(ab.group(2)), abonos_count=int(ab.group(1)),
            cargos_total=parse_mx_amount(ca.group(2)), cargos_count=int(ca.group(1)),
            saldo_anterior=parse_mx_amount(need(_RE_SALDO_ANT, "Saldo Anterior").group(1)),
            saldo_final=parse_mx_amount(need(_RE_SALDO_FIN, "Saldo Final").group(1)),
        )

    @staticmethod
    def _resolve_year(month: int, period) -> int:
        """Año desde el periodo. Maneja cruce dic→ene: mes del inicio → año inicio, etc."""
        start_month, start_year, end_month, end_year = period
        if month == start_month:
            return start_year
        if month == end_month:
            return end_year
        return start_year if month >= start_month else end_year

    @staticmethod
    def _group_movements(lines) -> list[list[list[dict]]]:
        """Un movimiento arranca en una línea con dos fechas (OPER LIQ) y se extiende por las
        líneas de continuación hasta la siguiente fecha o un terminador (footer/header/total)."""
        movements: list[list[list[dict]]] = []
        cur: list[list[dict]] | None = None
        for ws in lines:
            text = " ".join(w["text"] for w in ws)
            if any(t in text for t in _TERMINATORS):
                cur = None
                continue
            if len(ws) >= 2 and _DATE.match(ws[0]["text"]) and _DATE.match(ws[1]["text"]):
                cur = [ws]
                movements.append(cur)
            elif cur is not None:
                cur.append(ws)
        return movements

    @staticmethod
    def _movement_amount(mov, anchors) -> Decimal:
        """Toma el número alineado a CARGOS (−) o ABONOS (+). Saldos (OPER/LIQ) se ignoran."""
        for ws in mov:
            for w in ws:
                if not _NUM.match(w["text"]):
                    continue
                col = min(anchors, key=lambda k: abs(w["x1"] - anchors[k]))
                if col == "CARGOS":
                    return -parse_mx_amount(w["text"])
                if col == "ABONOS":
                    return parse_mx_amount(w["text"])
        raise ParseValidationError("Movimiento sin importe en columna CARGOS/ABONOS")

    @staticmethod
    def _movement_text(mov, ref_x0, cargos_x0) -> tuple[str, str | None]:
        """raw_payee = descripción de la primera línea; notes = resto de desc + referencias."""
        first_desc: str | None = None
        extra: list[str] = []
        for ws in mov:
            desc = [w for w in ws if w["x1"] < ref_x0 - 2 and not _DATE.match(w["text"]) and not _NUM.match(w["text"])]
            ref = [w for w in ws if ref_x0 - 2 <= w["x1"] < cargos_x0 and not _NUM.match(w["text"])]
            dtext = " ".join(w["text"] for w in desc)
            rtext = " ".join(w["text"] for w in ref)
            if dtext:
                if first_desc is None:
                    first_desc = dtext
                else:
                    extra.append(dtext)
            if rtext:
                extra.append(rtext)
        return (first_desc or "", "; ".join(extra) or None)
