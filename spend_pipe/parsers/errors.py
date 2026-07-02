"""Errores de parsing. Para finanzas: fallar fuerte > parsear mal en silencio."""
from __future__ import annotations


class ParseValidationError(Exception):
    """Los movimientos extraídos no cuadran contra el resumen del estado de cuenta."""


class LayoutNotRecognizedError(Exception):
    """El PDF no matchea el layout esperado (no se encontró header/periodo/tabla)."""
