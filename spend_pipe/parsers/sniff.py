"""Detección de archivos subidos: qué es, de qué banco, y si podemos ingerirlo.

Principio: detectar por CONTENIDO, nunca por extensión (el '.xls' de Openbank es HTML,
un '.pdf' puede ser cualquier banco). El flujo del upload único:

  bytes → sniff_kind (pdf/html/csv/xlsx) → fingerprint por fuente conocida →
    · parser disponible  → ingesta directa
    · fuente conocida sin parser → diagnóstico con guía (qué es, qué hacer)
    · CSV desconocido    → guess de columnas para el form de mapeo
    · resto              → pegado manual como salida

Las funciones de fingerprint son puras (reciben texto) para poder testearlas sin archivos.
"""
from __future__ import annotations

import csv as _csv
import io
import re
from dataclasses import dataclass, field


@dataclass
class Detection:
    kind: str                              # pdf | html | csv | xlsx | text | unknown
    source_bank: str | None = None
    format: str | None = None              # clave del registry cuando hay parser
    source_account: str | None = None
    label: str = "Formato no reconocido"
    parser_available: bool = False
    confidence: str = "low"                # high | medium | low
    guidance: str | None = None            # consejo cuando no hay ingesta directa
    csv_headers: list[str] = field(default_factory=list)
    csv_guess: dict[str, str | None] = field(default_factory=dict)   # rol → header
    csv_delimiter: str = ","
    csv_header_row: int = 0                # línea del header (hay exports con preámbulo)
    suggest_invert: bool = False           # tarjeta con gastos en positivo → invertir
    mapping_id: str | None = None          # match con un mapeo CSV guardado


# ── Tipo de archivo por magic bytes / contenido ─────────────────────────────
def sniff_kind(data: bytes) -> str:
    head = data[:4096].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    if head[:2] == b"PK":
        return "xlsx"      # zip: xlsx/ods real
    low = head[:512].lower()
    if low.startswith(b"<!doctype") or low.startswith(b"<html") or b"<table" in low:
        return "html"
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception:
            return "unknown"
    first = next((l for l in text.splitlines() if l.strip()), "")
    if "," in first or ";" in first or "\t" in first:
        return "csv"
    return "text"


# ── Fingerprints de PDF (sobre el texto de las primeras páginas) ───────────
def detect_pdf_text(text: str) -> Detection:
    t = " ".join(text.split())

    if "CARGOS" in t and "ABONOS" in t and ("Libretón" in t or "Cuenta Digital" in t):
        return Detection(
            kind="pdf", source_bank="bbva-cuenta-digital", format="pdf",
            source_account="BBVA Cuenta Digital", parser_available=True, confidence="high",
            label="BBVA Cuenta Digital — estado de cuenta (PDF)",
        )
    if "BBVA" in t and ("Puntos BBVA" in t or "tarjeta de crédito" in t.lower()):
        return Detection(
            kind="pdf", source_bank="bbva-tdc", source_account="BBVA TDC",
            label="BBVA Tarjeta de Crédito — estado de cuenta (PDF)", confidence="high",
            guidance=(
                "Todavía no hay parser para el PDF de la TDC. Mientras tanto: usá el "
                "pegado manual (date,amount,payee) con los movimientos del estado, "
                "como venías haciendo con el CSV a mano. El parser está en el roadmap."
            ),
        )
    # Ojo: 'Open Bank S.A.' viene en texto rotado (al revés) en el footer del PDF,
    # así que la segunda ancla es 'POSICION GLOBAL' (encabezado de la primera página).
    if "EXTRACTO UNIFICADO" in t and ("Open Bank" in t or "POSICION GLOBAL" in t):
        return Detection(
            kind="pdf", source_bank="openbank-extracto", source_account="Openbank Nómina",
            label="Openbank — Extracto Unificado (PDF)", confidence="high",
            guidance=(
                "⚠️ Este extracto de la cuenta corriente SOLAPA con 'Movimientos de "
                "Tarjeta' (la tarjeta es de débito y carga acá): importarlo duplicaría "
                "esos cargos. Hasta que exista dedup cross-source (MVP 2), conviene NO "
                "ingerir el extracto completo; los movimientos de tarjeta ya entran por el .xls."
            ),
        )
    if "Monthly Statement" in t and ("Market Value" in t or "Cash Summary" in t):
        return Detection(
            kind="pdf", source_bank="ruut-alpaca", source_account="RUUT Alpaca",
            label="RUUT / Alpaca — Monthly Statement (PDF)", confidence="high",
            guidance=(
                "Cuenta de inversión: va por el flujo de snapshot de saldo (ajuste "
                "mensual), no por el pipeline de movimientos. Ese flujo quedó fuera "
                "del MVP a propósito; actualizá el saldo en Actual como hasta ahora."
            ),
        )
    return Detection(
        kind="pdf", label="PDF no reconocido",
        guidance=(
            "No matchea ningún layout conocido. Opciones: usar el pegado manual "
            "(date,amount,payee), o pasarme el archivo para armar un parser si va a "
            "llegar todos los meses."
        ),
    )


# ── Fingerprint de HTML (el '.xls' de Openbank y similares) ────────────────
def detect_html_text(text: str) -> Detection:
    t = " ".join(text.split())
    if "Tarjetas - Movimientos" in t or ("Lista de Movimientos" in t and "Tarjeta" in t):
        return Detection(
            kind="html", source_bank="openbank-tdc", format="xls",
            source_account="Openbank Tarjeta", parser_available=True, confidence="high",
            label="Openbank — Movimientos de Tarjeta (.xls/HTML)",
        )
    return Detection(
        kind="html", label="Tabla HTML no reconocida",
        guidance="Es un HTML con tabla pero no matchea ninguna fuente conocida. "
                 "Si es un export mensual de un banco, pasámelo y armamos el parser.",
    )


# ── CSV: match exacto, mapeos guardados, o guess de columnas ────────────────
_DATE_HDRS = ("date", "fecha", "fecha operación", "f. operación", "fecha valor")
_AMOUNT_HDRS = ("amount", "importe", "monto", "cantidad", "valor")
_DEBIT_HDRS = ("retiro", "cargo", "cargos", "debit", "débito")
_CREDIT_HDRS = ("deposito", "depósito", "abono", "abonos", "credit", "ingreso")
_PAYEE_HDRS = ("payee", "concepto", "descripcion", "descripción", "description", "beneficiario", "comercio")
_MEMO_HDRS = ("memo", "notes", "notas", "referencia", "detalle", "observaciones", "consecutivo")
_DATE_VAL = re.compile(r"^\d{2,4}[-/][A-Za-z0-9]{2,3}[-/]\d{2,4}$")
_NUM_VAL = re.compile(r"^-?[\d.,$ ]+$")
_HDR_CELL = re.compile(r"^[A-Za-zÁÉÍÓÚÑáéíóúñ0-9 ._()/-]{1,40}$")


def find_header_line(lines: list[str], delimiter: str) -> int:
    """Índice de la línea que parece el header real (los exports traen preámbulo).

    Heurística: primera línea con ≥3 celdas tipo nombre-de-columna cuya línea
    siguiente tenga al menos la misma cantidad de celdas (datos consistentes).
    """
    for i, line in enumerate(lines[:30]):
        cells = [c.strip() for c in line.split(delimiter)]
        if len(cells) < 3:
            continue
        if not all(c and _HDR_CELL.match(c) and not any(ch.isdigit() for ch in c[:2]) for c in cells):
            continue
        if i + 1 < len(lines) and len(lines[i + 1].split(delimiter)) >= len(cells):
            return i
    return 0


def header_signature(headers: list[str]) -> str:
    """Firma estable de un layout CSV para reconocerlo en futuros uploads."""
    return "|".join(sorted(h.strip().lower() for h in headers))


def _guess_role(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    for h in headers:
        if h.strip().lower() in candidates:
            return h
    for h in headers:
        if any(c in h.strip().lower() for c in candidates):
            return h
    return None


def detect_csv_text(text: str) -> Detection:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return Detection(kind="csv", label="CSV vacío")
    delimiter = "\t" if "\t" in lines[0] else (";" if ";" in lines[0] and "," not in lines[0] else ",")

    header_row = find_header_line(lines, delimiter)
    reader = _csv.DictReader(io.StringIO("\n".join(lines[header_row:header_row + 20])), delimiter=delimiter)
    headers = [h.strip() for h in (reader.fieldnames or [])]
    rows = list(reader)
    hset = [h.lower() for h in headers]

    # ── Fingerprints exactos de formatos conocidos ──
    if headers == ["Date", "Payee", "Memo", "Amount"]:
        return Detection(
            kind="csv", source_bank="bbva-tdc", format="csv", source_account="BBVA TDC",
            parser_available=True, confidence="high",
            label="BBVA TDC — export CSV (Date, Payee, Memo, Amount)",
            csv_headers=headers, csv_delimiter=delimiter, csv_header_row=header_row,
        )
    if hset == ["fecha", "consecutivo", "concepto", "importe"]:
        # App BBVA MX, movimientos de tarjeta: preámbulo + cargos en positivo.
        return Detection(
            kind="csv", source_bank="bbva-mx-app",
            label="BBVA México — movimientos de tarjeta (export de la app)",
            confidence="high", csv_headers=headers, csv_delimiter=delimiter,
            csv_header_row=header_row, suggest_invert=True,
            csv_guess={"date": headers[0], "amount": headers[3], "payee": headers[2],
                       "memo": headers[1], "debit": None, "credit": None},
            guidance="Formato reconocido (tarjeta, app BBVA MX). Los cargos vienen en "
                     "positivo, así que sugiero invertir el signo. Confirmá el mapeo, "
                     "la cuenta destino, y podés guardarlo para que entre solo.",
        )
    if {"fecha", "concepto", "retiro", "deposito"} <= set(hset):
        # App BBVA MX, movimientos de cuenta: retiro/depósito en columnas separadas.
        by = dict(zip(hset, headers))
        return Detection(
            kind="csv", source_bank="bbva-mx-app",
            label="BBVA México — movimientos de cuenta (export de la app)",
            confidence="high", csv_headers=headers, csv_delimiter=delimiter,
            csv_header_row=header_row,
            csv_guess={"date": by["fecha"], "amount": None, "payee": by["concepto"],
                       "debit": by["retiro"], "credit": by["deposito"],
                       "memo": by.get("referencia")},
            guidance="Formato reconocido (cuenta, app BBVA MX): retiro y depósito en "
                     "columnas separadas; la columna SALDO se ignora. Confirmá el mapeo "
                     "y la cuenta destino.",
        )

    # ── Desconocido: guess por nombre de columna, fallback por forma de los valores ──
    guess = {
        "date": _guess_role(headers, _DATE_HDRS),
        "amount": _guess_role(headers, _AMOUNT_HDRS),
        "debit": _guess_role(headers, _DEBIT_HDRS),
        "credit": _guess_role(headers, _CREDIT_HDRS),
        "payee": _guess_role(headers, _PAYEE_HDRS),
        "memo": _guess_role(headers, _MEMO_HDRS),
    }
    if rows and (guess["date"] is None or (guess["amount"] is None and not guess["debit"])):
        for h in headers:
            vals = [r.get(h, "").strip() for r in rows[:5] if r.get(h, "").strip()]
            if not vals:
                continue
            if guess["date"] is None and all(_DATE_VAL.match(v) for v in vals):
                guess["date"] = h
            elif guess["amount"] is None and not guess["debit"] and h not in guess.values() \
                    and all(_NUM_VAL.match(v) for v in vals):
                guess["amount"] = h

    complete = guess["date"] and (guess["amount"] or guess["debit"]) and guess["payee"]
    return Detection(
        kind="csv", label="CSV no reconocido — mapeá las columnas",
        confidence="medium" if complete else "low",
        csv_headers=headers, csv_guess=guess, csv_delimiter=delimiter,
        csv_header_row=header_row,
        guidance="Decime qué columna es fecha, importe y concepto y lo ingiero. "
                 "Podés guardar el mapeo para que el próximo mes entre solo.",
    )


# ── Orquestador ──────────────────────────────────────────────────────────────
def detect_file(path: str) -> Detection:
    with open(path, "rb") as f:
        data = f.read()
    kind = sniff_kind(data)

    if kind == "pdf":
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            text = " ".join((p.extract_text() or "") for p in pdf.pages[:2])
        return detect_pdf_text(text)

    if kind == "html":
        return detect_html_text(data.decode("utf-8", errors="replace"))

    if kind in ("csv", "text"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return detect_csv_text(text)

    if kind == "xlsx":
        return Detection(
            kind="xlsx", label="Excel real (.xlsx)",
            guidance="Todavía no hay ingesta directa de .xlsx. Exportalo/guardalo como CSV "
                     "y subilo de nuevo, o pasámelo para armar el parser.",
        )
    return Detection(kind="unknown", guidance="No pude identificar el tipo de archivo.")
