"""Construcción de las dos claves de identidad del pipeline.

Son dos claves con trabajos distintos y NO comparten fórmula:

- `imported_id`  → viaja a Actual. Promesa de idempotencia: re-correr o re-parsear
  el MISMO source nunca debe doble-insertar. Se construye con campos RAW (lo que el
  banco mandó, que no cambia) + un índice de ocurrencia, para que:
    (a) mejorar la normalización más adelante no corra las identidades, y
    (b) duplicados legítimos (mismo día, monto y comercio) no se colapsen en uno.

- `dedup_hash`   → interno (MVP2). Atrapa la MISMA transacción real llegando de
  dos fuentes distintas (BBVA-PDF y TDC-CSV) o recargas. Usa campos NORMALIZADOS
  y es una *pista* para el review, no un drop automático.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

_WS = re.compile(r"\s+")
_SLUG = re.compile(r"[^a-z0-9]+")


def slug(value: str | None) -> str:
    """Forma slug estable para el segmento de cuenta del imported_id.

    'BBVA TDC' → 'bbva-tdc'. Es idempotente: pasar el slug de nuevo da lo mismo.
    Se decide una sola vez, antes del primer push, porque cambia identidades.
    """
    return _SLUG.sub("-", (value or "").strip().lower()).strip("-")


def canonicalize_payee(raw: str | None) -> str:
    """Forma estable de un payee: sin espacios redundantes, en mayúsculas.

    Se usa SOLO para hashear identidad, nunca para mostrar. Hace que
    'UBER  RIDE' y 'uber ride' produzcan la misma identidad.
    """
    return _WS.sub(" ", (raw or "").strip()).upper()


def amount_to_cents(amount) -> int:
    """Entero de centavos con signo. Mata el drift '532.0' vs '532.00'."""
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _payee_hash8(raw_payee: str | None) -> str:
    return hashlib.sha1(canonicalize_payee(raw_payee).encode("utf-8")).hexdigest()[:8]


def content_key(source_account: str, date_iso: str, amount, raw_payee: str | None) -> tuple:
    """Clave de contenido (sin ocurrencia) que define qué transacciones 'se ven iguales'."""
    return (slug(source_account), date_iso, amount_to_cents(amount), _payee_hash8(raw_payee))


def imported_id(source_account: str, date_iso: str, amount, raw_payee: str | None, occ: int) -> str:
    """Clave de idempotencia hacia Actual.

    Formato: spendpipe:{source_account}:{date}:{amount_cents}:{raw_payee_hash8}:{occ}
    Ej:      spendpipe:bbva-tdc:2026-05-05:-53200:9f2a1b3c:0
    """
    return f"spendpipe:{slug(source_account)}:{date_iso}:{amount_to_cents(amount)}:{_payee_hash8(raw_payee)}:{occ}"


def assign_imported_ids(rows: Iterable[tuple]) -> list[str]:
    """Asigna `imported_id` a una secuencia de filas de un source, calculando el
    índice de ocurrencia por grupo de clave-de-contenido y preservando el orden.

    Cada fila es la tupla (source_account, date_iso, amount, raw_payee).
    Dos filas idénticas dentro del mismo source → ...:0 y ...:1.
    """
    counts: dict[tuple, int] = defaultdict(int)
    out: list[str] = []
    for source_account, date_iso, amount, raw_payee in rows:
        k = content_key(source_account, date_iso, amount, raw_payee)
        occ = counts[k]
        counts[k] += 1
        out.append(imported_id(source_account, date_iso, amount, raw_payee, occ))
    return out


def dedup_hash(account: str, date_iso: str, amount, normalized_payee: str | None) -> str:
    """Hash interno para detección de duplicados cross-source (MVP2).

    Usa el payee NORMALIZADO a propósito: dos fuentes distintas escriben el mismo
    comercio de forma diferente en crudo, pero deberían normalizar igual.
    """
    basis = f"{account}|{date_iso}|{amount_to_cents(amount)}|{canonicalize_payee(normalized_payee)}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
