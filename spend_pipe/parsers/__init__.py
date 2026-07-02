"""Capa de parsers.

Todo parser convierte una fuente concreta a `CommonTransaction`. Aguas abajo nadie
sabe de qué formato vino (csv/xlsx/pdf/manual): solo ve el schema común.

El registry está indexado por (source_bank, format). PDF es una clave válida desde
el día uno aunque todavía no haya implementación: cuando llegue BBVA-PDF (MVP5) se
enchufa acá sin tocar nada aguas abajo.
"""
from .registry import available_parsers, get_parser, register  # noqa: F401

# Registrar los parsers conocidos al importar el paquete.
from . import configured  # noqa: F401,E402
