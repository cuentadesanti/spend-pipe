"""Parsers concretos ya configurados por banco. Se registran al importar el paquete.

Agregar un banco nuevo que exporta CSV = agregar una instancia acá. El primer parser
productivo es CSV/XLSX; el primer PDF productivo será BBVA (MVP5), que se registrará
igual pero con format='pdf'.
"""
from __future__ import annotations

from .csv_generic import CsvColumnMap, GenericCsvParser
from .openbank import OpenbankCardParser
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

# Openbank 'Movimientos de Tarjeta' — .xls que en realidad es HTML, importes en EUR.
# La cuenta destino en Actual se decide aparte (ver modelado tarjeta vs. Nómina *8579).
register(
    OpenbankCardParser(
        source_bank="openbank-tdc",
        source_account="Openbank Tarjeta",
        currency="EUR",
    )
)
