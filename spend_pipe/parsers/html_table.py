"""Extractor de filas de tablas HTML, con stdlib (sin lxml/bs4).

Varios bancos (Openbank, entre otros) exportan un '.xls' que en realidad es HTML.
Esto extrae las filas como listas de texto de celda; cada parser decide después
qué filas son datos y cómo mapear columnas.
"""
from __future__ import annotations

from html.parser import HTMLParser


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # decodifica &oacute; &nbsp; etc.
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def extract_rows(html: str) -> list[list[str]]:
    """Devuelve todas las filas <tr> como listas de texto de celda (ya sin espacios sobrantes)."""
    p = _TableRows()
    p.feed(html)
    return p.rows
