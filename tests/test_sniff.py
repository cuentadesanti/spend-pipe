"""Tests de la detección por contenido: tipo de archivo, fingerprints y guess de columnas."""
from spend_pipe.parsers.sniff import (
    detect_csv_text,
    detect_html_text,
    detect_pdf_text,
    header_signature,
    sniff_kind,
)


# ── Tipo de archivo por contenido, no extensión ──────────────────────────────
def test_kind_pdf():
    assert sniff_kind(b"%PDF-1.4 blah") == "pdf"


def test_kind_html_disguised_as_xls():
    # El caso Openbank: '.xls' que es HTML.
    assert sniff_kind(b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0"><html>...') == "html"


def test_kind_csv():
    assert sniff_kind(b"Date,Payee,Memo,Amount\n2026-05-05,X,, -1.00\n") == "csv"


def test_kind_xlsx_zip():
    assert sniff_kind(b"PK\x03\x04rest-of-zip") == "xlsx"


# ── Fingerprints de PDF ──────────────────────────────────────────────────────
def test_pdf_bbva_cuenta_digital_detected_with_parser():
    d = detect_pdf_text("Estado de Cuenta Libretón Básico Cuenta Digital ... CARGOS ABONOS ...")
    assert d.source_bank == "bbva-cuenta-digital"
    assert d.parser_available is True
    assert d.format == "pdf"


def test_pdf_bbva_tdc_detected_with_parser():
    d = detect_pdf_text("BBVA ... Tu tarjeta de crédito te abre un mundo ... Puntos BBVA ...")
    assert d.source_bank == "bbva-tdc"
    assert d.format == "pdf"
    assert d.parser_available is True   # parser de TDC PDF disponible desde 2026-07


def test_pdf_openbank_extracto_warns_about_overlap():
    d = detect_pdf_text("EXTRACTO UNIFICADO ... Open Bank S.A. ... POSICION GLOBAL")
    assert d.source_bank == "openbank-extracto"
    assert d.parser_available is False
    assert "SOLAPA" in d.guidance or "duplicar" in d.guidance


def test_pdf_ruut_alpaca_routed_to_snapshot():
    d = detect_pdf_text("SANTIAGO SILVA Monthly Statement Period: MAY - 2026 Cash Summary Total Market Value")
    assert d.source_bank == "ruut-alpaca"
    assert "snapshot" in d.guidance


def test_pdf_unknown_has_fallback_guidance():
    d = detect_pdf_text("Factura electrónica CFDI algo totalmente distinto")
    assert d.source_bank is None
    assert d.guidance


# ── HTML ─────────────────────────────────────────────────────────────────────
def test_html_openbank_card_detected():
    d = detect_html_text("<td>Tarjetas - Movimientos</td><td>Lista de Movimientos</td>")
    assert d.source_bank == "openbank-tdc"
    assert d.format == "xls"
    assert d.parser_available is True


def test_html_openbank_account_detected_not_confused_with_card():
    # El extracto de cuenta también dice 'TARJETA' en los conceptos (Apple Pay);
    # debe detectarse como cuenta, no como tarjeta.
    d = detect_html_text(
        "<td>Cuentas - Movimientos</td><td>Lista de Movimientos</td>"
        "<td>Apple pay: COMPRA EN UBER, CON LA TARJETA : 5489</td>"
    )
    assert d.source_bank == "openbank-cuenta"
    assert d.format == "xls"
    assert d.parser_available is True


# ── CSV ──────────────────────────────────────────────────────────────────────
def test_csv_bbva_tdc_exact_match():
    d = detect_csv_text("Date,Payee,Memo,Amount\n2026-05-05,AT T CR,, -532.00\n")
    assert d.source_bank == "bbva-tdc"
    assert d.parser_available is True


def test_csv_unknown_guesses_columns_by_name():
    d = detect_csv_text("Fecha,Concepto,Importe\n2026-05-05,SUPER,-100.00\n")
    assert d.parser_available is False
    assert d.csv_guess["date"] == "Fecha"
    assert d.csv_guess["payee"] == "Concepto"
    assert d.csv_guess["amount"] == "Importe"
    assert d.confidence == "medium"


def test_csv_unknown_guesses_by_value_shape():
    # Headers crípticos: cae al fallback por forma de los valores.
    d = detect_csv_text("col1,col2,col3\n2026-05-05,COMPRA X,-12.50\n2026-05-06,COMPRA Y,-9.00\n")
    assert d.csv_guess["date"] == "col1"
    assert d.csv_guess["amount"] == "col3"


def test_header_signature_stable():
    assert header_signature(["Date", "Amount"]) == header_signature(["amount", "date"])


# ── Exports con preámbulo y columnas separadas (formato app BBVA MX) ─────────
_TARJETA_APP = """No. de Tarjeta: 1111**2222
Producto: EJEMPLO REWARDS
TASA DE INTERÉS: 25.27 %
Detalle del 01/jun/2026 al 26/jun/2026,Total de movimientos: 2
FECHA,CONSECUTIVO,CONCEPTO,IMPORTE
20/Jun/2026,111,COMERCIO UNO,$ 692.81
24/Jun/2026,222,COMERCIO DOS,$ 1,029.25
"""

_CUENTA_APP = """Detalle del 01/jun/2026 al 26/jun/2026 de la cuenta **9999,Número de registros: 2
FECHA,HORA,SUCURSAL,CONCEPTO,RETIRO,DEPOSITO,SALDO,REFERENCIA
22/Jun/2026,21:45:56,5234,Retiro sin tarjeta,$ -2000,-,$ 58456.7,30792670
19/Jun/2026,22:13:34,3597,Transferencia recibida,-,$ 43000,$ 60456.7,002750481
"""


def test_csv_preamble_header_found_and_tarjeta_fingerprint():
    d = detect_csv_text(_TARJETA_APP)
    assert d.csv_header_row == 4                 # saltó el preámbulo
    assert d.source_bank == "bbva-mx-app"
    assert d.csv_guess["amount"] == "IMPORTE"
    assert d.suggest_invert is True              # tarjeta: cargos en positivo


def test_csv_cuenta_fingerprint_with_debit_credit_split():
    d = detect_csv_text(_CUENTA_APP)
    assert d.source_bank == "bbva-mx-app"
    assert d.csv_guess["debit"] == "RETIRO"
    assert d.csv_guess["credit"] == "DEPOSITO"
    assert d.csv_guess["amount"] is None
    assert d.suggest_invert is False
