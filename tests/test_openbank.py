"""Test del parser Openbank (HTML disfrazado de .xls) y del parseo de importes EU."""
from datetime import date
from decimal import Decimal

from spend_pipe.parsers import get_parser
from spend_pipe.parsers.openbank import OpenbankCardParser, parse_eu_amount

# Estructura real de Openbank: 13 celdas por fila con espaciadoras vacías intercaladas.
def _row(fecha, hora, concepto, situacion, localidad, importe, divisa):
    cells = ["", fecha, "", hora, "", concepto, "", situacion, "", localidad, "", importe, divisa]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

HTML = (
    "<html><body><table>"
    "<tr><td>Tarjetas - Movimientos</td></tr>"  # preámbulo, se ignora
    "<tr><td>Fecha</td><td>Hora</td><td>Concepto</td><td>Importe</td></tr>"  # cabecera, se ignora
    + _row("01-07-2026", "22:01", "COMIDA GUAU TAP", "AUTORIZADO", "CIUDAD DE MEX", "-11,83", "EUR")  # pending
    + _row("02-07-2026", "01:05", "IBERIA LAE SA O", "LIQUIDADO", "", "-1.163,33", "EUR")   # miles + localidad vacía
    + _row("03-07-2026", "10:00", "DEVOLUCION", "LIQUIDADO", "MADRID", "50,00", "EUR")       # positivo, liquidado
    + "</table></body></html>"
)


def test_parse_eu_amount():
    assert parse_eu_amount("-11,83") == Decimal("-11.83")
    assert parse_eu_amount("-1.163,33") == Decimal("-1163.33")   # punto = miles, coma = decimal
    assert parse_eu_amount("50,00") == Decimal("50.00")


def test_openbank_parser_registered():
    p = get_parser("openbank-tdc", "xls")
    assert p.format == "xls"


def test_openbank_parse(tmp_path):
    f = tmp_path / "mov.xls"
    f.write_text(HTML, encoding="utf-8")
    txns = OpenbankCardParser("openbank-tdc", "Openbank Tarjeta", "EUR").parse(str(f))

    assert len(txns) == 3
    assert txns[0].date == date(2026, 7, 1)
    assert txns[0].amount == Decimal("-11.83")
    assert txns[0].raw_payee == "COMIDA GUAU TAP"
    assert txns[0].currency == "EUR"
    assert txns[0].notes == "CIUDAD DE MEX · AUTORIZADO"

    # Miles parseados + localidad vacía no descoloca las columnas (índices fijos).
    assert txns[1].amount == Decimal("-1163.33")
    assert txns[1].notes == "LIQUIDADO"   # sin localidad, solo situación

    # Devolución positiva.
    assert txns[2].amount == Decimal("50.00")


def test_situacion_sets_pending():
    # AUTORIZADO → pending; LIQUIDADO → definitivo.
    import tempfile, os
    d = tempfile.mkdtemp()
    p = os.path.join(d, "mov.xls")
    open(p, "w", encoding="utf-8").write(HTML)
    txns = OpenbankCardParser("openbank-tdc", "Openbank Tarjeta", "EUR").parse(p)
    assert txns[0].pending is True    # AUTORIZADO
    assert txns[1].pending is False   # LIQUIDADO
    assert txns[2].pending is False   # LIQUIDADO


def test_pending_forces_needs_review():
    from datetime import date
    from spend_pipe.models import Transaction, TxnStatus
    from spend_pipe.pipeline.normalize import run_pipeline

    base = dict(date=date(2026, 7, 1), amount=Decimal("-11.83"), currency="EUR", raw_payee="COMIDA GUAU TAP")
    liquidado = run_pipeline(Transaction(**base, pending=False))
    autorizado = run_pipeline(Transaction(**base, pending=True))
    assert liquidado.status == TxnStatus.normalized
    assert autorizado.status == TxnStatus.needs_review
