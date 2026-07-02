"""Registry de parsers indexado por (source_bank, format)."""
from __future__ import annotations

from .base import Parser

_REGISTRY: dict[tuple[str, str], Parser] = {}


def register(parser: Parser) -> Parser:
    """Registra una instancia de parser. Falla si ya hay una para la misma clave."""
    key = (parser.source_bank, parser.format)
    if key in _REGISTRY:
        raise ValueError(f"Ya existe un parser para {key}")
    _REGISTRY[key] = parser
    return parser


def get_parser(source_bank: str, format: str) -> Parser:
    try:
        return _REGISTRY[(source_bank, format)]
    except KeyError:
        disponibles = ", ".join(f"{b}/{f}" for b, f in sorted(_REGISTRY)) or "(ninguno)"
        raise LookupError(
            f"No hay parser para {source_bank}/{format}. Disponibles: {disponibles}"
        ) from None


def available_parsers() -> list[tuple[str, str]]:
    return sorted(_REGISTRY)
