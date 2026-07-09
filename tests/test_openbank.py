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


# ── Extracto de cuenta ('Cuentas - Movimientos') ─────────────────────────────
# Estructura real: 10 celdas por fila, datos en índices impares (1,3,5,7,9).
def _acc_row(fecha_op, fecha_valor, concepto, importe, saldo):
    cells = ["", fecha_op, "", fecha_valor, "", concepto, "", importe, "", saldo]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"

def _acc_html(rows, saldo_header="80,05 EUR"):
    return (
        "<html><body><table>"
        "<tr><td></td><td>Cuentas - Movimientos</td></tr>"
        f"<tr><td></td><td>Saldo:</td><td></td><td>{saldo_header}</td></tr>"
        "<tr><td></td><td>Lista de Movimientos</td></tr>"
        "<tr><td></td><td>Fecha Operación</td><td></td><td>Fecha Valor</td><td></td>"
        "<td>Concepto</td><td></td><td>Importe</td><td></td><td>Saldo</td></tr>"
        + "".join(rows) + "</table></body></html>"
    )

# Más reciente primero, como lo exporta Openbank. Saldo corrido consistente.
ACC_ROWS = [
    _acc_row("08/07/2026", "07/07/2026", "COMISION POR COMPRAS REALIZADAS EN MONEDA NO EURO", "-0,35", "80,05"),
    _acc_row("07/07/2026", "07/07/2026", "TRANSFERENCIA INMEDIATA DE ACME SL CONCEPTO Nomina", "2.307,10", "80,40"),
    _acc_row("06/07/2026", "06/07/2026", "Apple pay: COMPRA EN UBER *EATS, CON LA TARJETA : 5489", "-11,86", "-2.226,70"),
]


def test_openbank_account_parser_registered():
    p = get_parser("openbank-cuenta", "xls")
    assert p.format == "xls"


def test_openbank_account_parse(tmp_path):
    from spend_pipe.parsers.openbank import OpenbankAccountParser

    f = tmp_path / "Movimientos de Cuenta.xls"
    f.write_text(_acc_html(ACC_ROWS), encoding="utf-8")
    txns = OpenbankAccountParser("openbank-cuenta", "Openbank Cuenta", "EUR").parse(str(f))

    assert len(txns) == 3
    assert txns[0].date == date(2026, 7, 8)
    assert txns[0].amount == Decimal("-0.35")
    assert txns[0].notes == "valor 07/07/2026"     # fecha valor distinta → nota
    assert txns[0].pending is False
    assert txns[1].amount == Decimal("2307.10")    # miles con punto
    assert txns[1].notes is None                   # misma fecha valor → sin nota
    assert txns[2].raw_payee.startswith("Apple pay: COMPRA EN UBER")
    assert all(t.currency == "EUR" for t in txns)


def test_openbank_account_balance_chain_lock(tmp_path):
    import pytest
    from spend_pipe.parsers.errors import ParseValidationError
    from spend_pipe.parsers.openbank import OpenbankAccountParser

    # Saldo corrido roto (fila intermedia perdida/deformada) → fallar fuerte.
    rows = [
        _acc_row("08/07/2026", "08/07/2026", "COMISION", "-0,35", "80,05"),
        _acc_row("07/07/2026", "07/07/2026", "NOMINA", "2.307,10", "99,99"),  # 99,99 + -0,35 != 80,05
    ]
    f = tmp_path / "mov.xls"
    f.write_text(_acc_html(rows), encoding="utf-8")
    with pytest.raises(ParseValidationError, match="no encadena"):
        OpenbankAccountParser("openbank-cuenta", "Openbank Cuenta", "EUR").parse(str(f))


def test_openbank_account_header_saldo_lock(tmp_path):
    import pytest
    from spend_pipe.parsers.errors import ParseValidationError
    from spend_pipe.parsers.openbank import OpenbankAccountParser

    # El saldo del encabezado no coincide con la primera fila → fallar fuerte.
    f = tmp_path / "mov.xls"
    f.write_text(_acc_html(ACC_ROWS, saldo_header="999,99 EUR"), encoding="utf-8")
    with pytest.raises(ParseValidationError, match="encabezado"):
        OpenbankAccountParser("openbank-cuenta", "Openbank Cuenta", "EUR").parse(str(f))


def test_pending_forces_needs_review():
    from datetime import date
    from spend_pipe.models import Transaction, TxnStatus
    from spend_pipe.pipeline.normalize import run_pipeline

    # Payee que matchea una regla (UBER → Transporte), para aislar el efecto de pending.
    base = dict(date=date(2026, 7, 1), amount=Decimal("-11.83"), currency="EUR", raw_payee="UBER RIDE")
    liquidado = run_pipeline(Transaction(**base, pending=False))
    autorizado = run_pipeline(Transaction(**base, pending=True))
    assert liquidado.status == TxnStatus.normalized      # regla + liquidado → fluye
    assert autorizado.status == TxnStatus.needs_review   # pending gatea aunque haya regla
