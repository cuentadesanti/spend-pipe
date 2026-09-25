"""POST /api/parse: archivo → extracto normalizado, sin tocar staging.

Es el contrato con ledger-guard. No guarda nada: detecta, parsea, calcula
`imported_id` con la misma fórmula que el ingest y devuelve JSON con los
importes en céntimos enteros.
"""
from __future__ import annotations

import hmac
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .identity import amount_to_cents, assign_imported_ids
from .parsers import get_parser
from .parsers.errors import LayoutNotRecognizedError, ParseValidationError
from .parsers.sniff import detect_file
from .statement import parse_statement

PARSE_SCHEMA_VERSION = "parse-1"
router = APIRouter()


def _cents(value: Decimal | None) -> int | None:
    return None if value is None else amount_to_cents(value)


def _check_token(authorization: str | None) -> None:
    expected = os.environ.get("SPENDPIPE_PARSE_TOKEN")
    if not expected:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def parse_file(path: str) -> tuple[int, dict]:
    det = detect_file(path)
    if not det.parser_available:
        return 422, {"error": "unsupported", "label": det.label, "guidance": det.guidance}
    parser = get_parser(det.source_bank, det.format)
    try:
        parsed = parse_statement(parser, path)
    except (ParseValidationError, LayoutNotRecognizedError) as exc:
        return 422, {"error": "parse_failed", "message": str(exc)}
    txns = parsed.transactions
    if not txns:
        return 422, {"error": "empty", "message": "El extracto no tiene movimientos."}

    period_from, period_to = parsed.period()
    ids = assign_imported_ids([(t.source_account, t.date.isoformat(), t.amount, t.raw_payee) for t in txns])
    return 200, {
        "schemaVersion": PARSE_SCHEMA_VERSION,
        "institution": det.source_bank,
        "accountHint": txns[0].source_account,
        "currency": txns[0].currency,
        "period": {"from": period_from.isoformat(), "to": period_to.isoformat()},
        "openingBalance": _cents(parsed.meta.opening_balance),
        "endingBalance": _cents(parsed.meta.ending_balance),
        "rows": [
            {
                "date": t.date.isoformat(),
                "amount": amount_to_cents(t.amount),
                "payee": t.raw_payee,
                "memo": t.notes,
                "importedId": imported,
                "pending": t.pending,
            }
            for t, imported in zip(txns, ids)
        ],
    }


@router.post("/api/parse")
def parse_endpoint(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    _check_token(authorization)
    suffix = Path(file.filename or "upload").suffix
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"statement{suffix}"
        with path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        status, body = parse_file(str(path))
    return JSONResponse(status_code=status, content=body)
