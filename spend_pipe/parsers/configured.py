"""Parsers concretos ya configurados por banco. Se registran al importar el paquete.

Agregar un banco nuevo que exporta CSV = agregar una instancia acá. El primer parser
productivo es CSV/XLSX; el primer PDF productivo será BBVA (MVP5), que se registrará
igual pero con format='pdf'.
"""
from __future__ import annotations

from .bbva_pdf import BbvaPdfParser
from .bbva_tdc_pdf import BbvaTdcPdfParser
from .csv_generic import CsvColumnMap, GenericCsvParser
from .openbank import OpenbankAccountParser, OpenbankCardParser
from .registry import register

# BBVA Tarjeta de Crédito — export CSV con columnas Date, Payee, Memo, Amount (fecha ISO).
register(
    GenericCsvParser(
        source_bank="bbva-tdc",
        source_account="BBVA TDC",
        currency="MXN",
        colmap=CsvColumnMap(date="Date", amount="Amount", payee="Payee", memo="Memo"),
    )
)

# BBVA Cuenta Digital (débito) — estado de cuenta PDF. source_account SIN CLABE/nº completo.
register(
    BbvaPdfParser(
        source_bank="bbva-cuenta-digital",
        source_account="BBVA Cuenta Digital",
        currency="MXN",
    )
)

# BBVA TDC — estado de cuenta PDF de la tarjeta de crédito. Mismo source_account que
# el CSV ('BBVA TDC'): una compra que llegue por ambas fuentes produce el MISMO
# imported_id y se deduplica sola en ingest.
register(
    BbvaTdcPdfParser(
        source_bank="bbva-tdc",
        source_account="BBVA TDC",
        currency="MXN",
    )
)

# Openbank 'Movimientos de Tarjeta' — .xls que en realidad es HTML, importes en EUR.
# La cuenta destino en Actual se decide aparte (ver modelado tarjeta vs. Nómina *8579).
register(
    OpenbankCardParser(
        source_bank="openbank-tdc",
        source_account="Openbank Tarjeta",
        currency="EUR",
    )
)

# Openbank 'Cuentas - Movimientos' — extracto de la cuenta corriente, mismo formato
# HTML-en-.xls. Cubre lo que la tarjeta no ve (nóminas, Bizum, transferencias,
# comisiones) y también los cargos de tarjeta con fecha de liquidación; el solape
# con la tarjeta y con lo legacy lo resuelve la reconciliación (tiers + adopción).
register(
    OpenbankAccountParser(
        source_bank="openbank-cuenta",
        source_account="Openbank Cuenta",
        currency="EUR",
    )
)
