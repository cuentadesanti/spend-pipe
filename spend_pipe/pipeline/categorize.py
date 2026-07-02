"""Pasada 3 del pipeline: categorización determinista por reglas YAML.

Las reglas viven en rules/categorize.yaml (versionadas en git — fuente de verdad
única, ya no en el servidor de Actual). Semántica idéntica a Actual: se aplican
TODAS las que matchean en orden pre → normal → post y la última gana.

Match determinista → confidence 1.0. Sin match → (None, None) y la fila cae a
needs_review (salvo transferencias, que no necesitan categoría). El fallback de
IA (módulo futuro) solo actuará sobre lo que salga de acá sin match, con
confidence < 1.0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import settings

_STAGES = ("pre", "normal", "post")

# Prefijos de wallet que esconden al comercio real ('Apple pay: DISTRITO BURGE').
# Se quitan ANTES de matchear, para que el comercio matchee su propia regla y no
# la del wallet (bug heredado de rules.js: keyword APPLE se tragaba todo Apple Pay).
_WALLET_PREFIX = re.compile(r"^\s*(apple pay|google pay|samsung pay)\s*:\s*", re.IGNORECASE)


def strip_wallet_prefix(raw_payee: str) -> str:
    return _WALLET_PREFIX.sub("", raw_payee or "")


@dataclass(frozen=True)
class Rule:
    label: str
    any: tuple[str, ...]
    none: tuple[str, ...] = ()
    payee: str | None = None
    category: str | None = None
    stage: str = "normal"

    def matches(self, raw_payee: str) -> bool:
        t = (raw_payee or "").upper()
        if any(k.upper() in t for k in self.none):
            return False
        return any(k.upper() in t for k in self.any)


@dataclass
class Verdict:
    category: str | None = None
    confidence: float | None = None
    payee_override: str | None = None
    rule_label: str | None = None


def _rules_path() -> str:
    return settings.rules_file


@lru_cache(maxsize=4)
def load_rules(path: str) -> tuple[Rule, ...]:
    p = Path(path)
    if not p.exists():
        return ()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    rules = []
    for r in raw:
        rules.append(Rule(
            label=r["label"],
            any=tuple(r.get("any", ())),
            none=tuple(r.get("none", ())),
            payee=r.get("payee"),
            category=r.get("category"),
            stage=r.get("stage", "normal"),
        ))
    return tuple(rules)


def classify(raw_payee: str, rules_path: str | None = None) -> Verdict:
    """Aplica todas las reglas que matchean, pre → normal → post; la última gana."""
    rules = load_rules(rules_path or _rules_path())
    payee = strip_wallet_prefix(raw_payee)
    verdict = Verdict()
    for stage in _STAGES:
        for rule in rules:
            if rule.stage != stage or not rule.matches(payee):
                continue
            verdict.category = rule.category
            verdict.confidence = 1.0     # determinista
            verdict.rule_label = rule.label
            if rule.payee:
                verdict.payee_override = rule.payee
    return verdict
