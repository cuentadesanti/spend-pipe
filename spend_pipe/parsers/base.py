"""Interfaz común de los parsers."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema import CommonTransaction


@runtime_checkable
class Parser(Protocol):
    """Contrato mínimo. Cada parser declara qué (source_bank, format) maneja."""

    source_bank: str
    format: str

    def parse(self, file_path: str) -> list[CommonTransaction]:
        """Lee el archivo y devuelve transacciones en schema común.

        No asigna `imported_id` (eso es responsabilidad del paso de ingest, que
        necesita ver todas las filas del archivo para calcular el índice de ocurrencia).
        """
        ...
