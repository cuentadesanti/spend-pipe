# spend-pipe — Plan 2 de Ledger Guard: `/api/parse`, saldos del extracto y Revolut

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Que spend-pipe exponga un endpoint sin estado `POST /api/parse` que convierta un extracto (CSV/PDF/HTML) en filas normalizadas con `imported_id`, periodo y saldo final, incluido un parser nuevo para el CSV de Revolut.

**Architecture:** `parse()` de los parsers no cambia, así que staging, review y push siguen igual. Un módulo nuevo `spend_pipe/statement.py` añade `ParsedFile` (transacciones + `StatementMeta`) y `parse_statement(parser, path)`, que usa el método opcional `parser.parse_statement()` cuando existe. El PDF de BBVA y el parser nuevo de Revolut lo implementan. `spend_pipe/api_parse.py` define el router y `spend_pipe/parse_app.py` es una app FastAPI mínima, sin base de datos, para desplegarla como servicio aparte.

**Tech Stack:** Python 3.13, FastAPI, pydantic 2, pdfplumber, pytest, httpx (para `TestClient`).

**Contrato con ledger-guard** (spec `ledger-guard/docs/superpowers/specs/2026-09-25-agentic-harness-design.md` §6): importes en céntimos enteros, fechas ISO, `imported_id` calculado con la misma fórmula que el ingest (`assign_imported_ids`).

## Global Constraints

- No cambiar la firma ni el comportamiento de `Parser.parse()`: el pipeline existente y sus 84 tests siguen pasando.
- Fallar fuerte antes que parsear mal: los errores de parseo son `ParseValidationError` o `LayoutNotRecognizedError` y el endpoint los devuelve como 422.
- Revolut: solo filas completadas (`COMPLETADO`/`COMPLETED`); importe neto = Importe − Comisión; la cadena de saldos debe cuadrar al céntimo.
- `SPENDPIPE_PARSE_TOKEN`: si está definido, `/api/parse` exige `Authorization: Bearer <token>`.
- Rama `feat/parse-api`. Cada commit termina con `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests: `.venv/bin/python -m pytest -q`.

## Mapa de archivos

| Archivo | Responsabilidad |
|---|---|
| `spend_pipe/statement.py` (nuevo) | `StatementMeta`, `ParsedFile`, `parse_statement()` |
| `spend_pipe/parsers/bbva_pdf.py` | `parse_statement()` con periodo exacto y saldos; `statement_period()` |
| `spend_pipe/parsers/revolut.py` (nuevo) | `RevolutCsvParser`, `revolut_columns()` |
| `spend_pipe/parsers/configured.py` | Registrar Revolut |
| `spend_pipe/parsers/sniff.py` | Detectar el CSV de Revolut |
| `spend_pipe/api_parse.py` (nuevo) | Router `/api/parse` y `parse_file()` |
| `spend_pipe/parse_app.py` (nuevo) | App mínima para el servicio de parseo |
| `tests/test_statement.py`, `tests/test_revolut.py`, `tests/test_api_parse.py` (nuevos), `tests/test_bbva_pdf.py` | Tests |

---

### Task 1: Metadatos del extracto

**Files:**
- Create: `spend_pipe/statement.py`
- Test: `tests/test_statement.py`

**Interfaces:**
- Produces: `StatementMeta(period_from=None, period_to=None, opening_balance=None, ending_balance=None)`, `ParsedFile(transactions, meta=StatementMeta())` con `period() -> tuple[date, date]`, `parse_statement(parser, file_path) -> ParsedFile`.

- [ ] **Step 1: Crear la rama**

```bash
git checkout -b feat/parse-api
```

- [ ] **Step 2: Escribir el test que falla**

`tests/test_statement.py`:

```python
from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers import get_parser
from spend_pipe.schema import CommonTransaction
from spend_pipe.statement import ParsedFile, StatementMeta, parse_statement


def _txn(day: int) -> CommonTransaction:
    return CommonTransaction(
        source_bank="x", source_account="X", format="csv", date=date(2026, 9, day),
        amount=Decimal("-1.00"), currency="EUR", raw_payee="p",
    )


def test_parser_without_metadata_gets_period_from_dates(tmp_path):
    path = tmp_path / "tdc.csv"
    path.write_text("Date,Payee,Memo,Amount\n2026-09-03,OXXO,,-10.00\n2026-09-01,UBER,,-5.50\n")
    parsed = parse_statement(get_parser("bbva-tdc", "csv"), str(path))
    assert parsed.meta == StatementMeta()
    assert len(parsed.transactions) == 2
    assert parsed.period() == (date(2026, 9, 1), date(2026, 9, 3))


def test_explicit_period_wins_over_dates():
    parsed = ParsedFile([_txn(5)], StatementMeta(period_from=date(2026, 9, 1), period_to=date(2026, 9, 30)))
    assert parsed.period() == (date(2026, 9, 1), date(2026, 9, 30))


def test_period_without_dates_or_meta_fails():
    with pytest.raises(ValueError):
        ParsedFile([]).period()
```

- [ ] **Step 3: Ejecutar el test para verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_statement.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'spend_pipe.statement'`.

- [ ] **Step 4: Implementar**

`spend_pipe/statement.py`:

```python
"""Metadatos de un extracto, además de sus movimientos.

`Parser.parse()` sigue devolviendo solo transacciones: staging, review y push no
cambian. Los parsers que saben leer el periodo y los saldos del documento
implementan además `parse_statement()`; el resto obtiene un periodo a partir de
las fechas de sus movimientos y ningún saldo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from .schema import CommonTransaction


@dataclass(frozen=True)
class StatementMeta:
    period_from: date | None = None
    period_to: date | None = None
    opening_balance: Decimal | None = None
    ending_balance: Decimal | None = None


@dataclass(frozen=True)
class ParsedFile:
    transactions: list[CommonTransaction]
    meta: StatementMeta = field(default_factory=StatementMeta)

    def period(self) -> tuple[date, date]:
        """Periodo del documento si el parser lo leyó; si no, el rango de fechas de los movimientos."""
        dates = [t.date for t in self.transactions]
        start = self.meta.period_from or (min(dates) if dates else None)
        end = self.meta.period_to or (max(dates) if dates else None)
        if start is None or end is None:
            raise ValueError("El extracto no tiene movimientos ni periodo")
        return start, end


def parse_statement(parser, file_path: str) -> ParsedFile:
    method = getattr(parser, "parse_statement", None)
    if callable(method):
        return method(file_path)
    return ParsedFile(parser.parse(file_path))
```

- [ ] **Step 5: Ejecutar los tests**

Run: `.venv/bin/python -m pytest -q`
Expected: los 3 nuevos PASS y los 84 existentes siguen en verde (87 en total).

- [ ] **Step 6: Commit**

```bash
git add spend_pipe/statement.py tests/test_statement.py
git commit -m "feat(statement): ParsedFile with period and balances alongside transactions

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Periodo y saldos del PDF de BBVA

**Files:**
- Modify: `spend_pipe/parsers/bbva_pdf.py`
- Test: `tests/test_bbva_pdf.py`

**Interfaces:**
- Consumes: `ParsedFile`, `StatementMeta` (Task 1).
- Produces: `statement_period(text: str) -> tuple[date, date] | None` y `BbvaPdfParser.parse_statement(file_path) -> ParsedFile` con `opening_balance = saldo_anterior` y `ending_balance = saldo_final`. `parse()` delega en `parse_statement()`.

- [ ] **Step 1: Escribir los tests que fallan**

Añadir al final de `tests/test_bbva_pdf.py`:

```python
def test_statement_period_reads_exact_days():
    from datetime import date
    from spend_pipe.parsers.bbva_pdf import statement_period

    text = "Periodo DEL 28/05/2026 AL 27/06/2026 Fecha de corte"
    assert statement_period(text) == (date(2026, 5, 28), date(2026, 6, 27))
    assert statement_period("sin periodo") is None


@pytest.mark.skipif(not os.path.exists(REAL_PDF), reason="PDF real no disponible")
def test_real_statement_exposes_balances_and_period():
    from spend_pipe.parsers import get_parser

    parsed = get_parser("bbva-cuenta-digital", "pdf").parse_statement(REAL_PDF)
    total = sum(t.amount for t in parsed.transactions)
    assert parsed.meta.opening_balance is not None and parsed.meta.ending_balance is not None
    assert parsed.meta.opening_balance + total == parsed.meta.ending_balance
    start, end = parsed.period()
    assert start <= min(t.date for t in parsed.transactions)
    assert end >= max(t.date for t in parsed.transactions)
```

- [ ] **Step 2: Ejecutar los tests para verificar que fallan**

Run: `.venv/bin/python -m pytest tests/test_bbva_pdf.py -q`
Expected: FAIL con `ImportError: cannot import name 'statement_period'`.

- [ ] **Step 3: Implementar**

En `spend_pipe/parsers/bbva_pdf.py`:

1. Junto a los otros imports del paquete, añadir:

```python
from ..statement import ParsedFile, StatementMeta
```

2. Justo debajo de la línea `_PERIODO = re.compile(...)`, añadir:

```python
_PERIODO_FECHAS = re.compile(r"DEL\s+(\d{2})/(\d{2})/(\d{4})\s+AL\s+(\d{2})/(\d{2})/(\d{4})")


def statement_period(text: str) -> tuple[date, date] | None:
    """Fechas exactas del 'Periodo DEL dd/mm/aaaa AL dd/mm/aaaa'."""
    m = _PERIODO_FECHAS.search(text)
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = (int(g) for g in m.groups())
    return date(y1, m1, d1), date(y2, m2, d2)
```

3. En `BbvaPdfParser`, cambiar la firma

```python
    def parse(self, file_path: str) -> list[CommonTransaction]:
```

por

```python
    def parse(self, file_path: str) -> list[CommonTransaction]:
        return self.parse_statement(file_path).transactions

    def parse_statement(self, file_path: str) -> ParsedFile:
```

4. Al final de ese método, reemplazar

```python
        validate_against_summary(txns, summary)   # el candado
        return txns
```

por

```python
        validate_against_summary(txns, summary)   # el candado
        period_dates = statement_period(full_text)
        return ParsedFile(
            txns,
            StatementMeta(
                period_from=period_dates[0] if period_dates else None,
                period_to=period_dates[1] if period_dates else None,
                opening_balance=summary.saldo_anterior,
                ending_balance=summary.saldo_final,
            ),
        )
```

- [ ] **Step 4: Ejecutar los tests**

Run: `.venv/bin/python -m pytest -q`
Expected: todos PASS. `test_real_statement_parses_and_reconciles` sigue pasando, porque `parse()` devuelve las mismas transacciones.

- [ ] **Step 5: Commit**

```bash
git add spend_pipe/parsers/bbva_pdf.py tests/test_bbva_pdf.py
git commit -m "feat(bbva-pdf): expose exact period and opening/ending balances

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Parser CSV de Revolut

**Files:**
- Create: `spend_pipe/parsers/revolut.py`
- Modify: `spend_pipe/parsers/configured.py`, `spend_pipe/parsers/sniff.py`
- Test: `tests/test_revolut.py`

**Interfaces:**
- Consumes: `ParsedFile`, `StatementMeta` (Task 1); `register`, `LayoutNotRecognizedError`, `ParseValidationError`.
- Produces: `revolut_columns(headers) -> dict[str, str] | None`, `COMPLETED_STATES`, `RevolutCsvParser` (`source_bank="revolut"`, `format="csv"`), registrado. `detect_csv_text` reconoce Revolut con `parser_available=True`.

- [ ] **Step 1: Escribir el test que falla**

`tests/test_revolut.py`:

```python
"""Parser del CSV de Revolut (cabeceras en español o inglés)."""
import csv
import os
from datetime import date
from decimal import Decimal

import pytest

from spend_pipe.parsers import get_parser
from spend_pipe.parsers.errors import ParseValidationError
from spend_pipe.parsers.revolut import RevolutCsvParser
from spend_pipe.parsers.sniff import detect_csv_text

ES_HEADER = "Tipo,Producto,Fecha de inicio,Fecha de finalización,Descripción,Importe,Comisión,Divisa,Estado,Saldo"
EN_HEADER = "Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance"
ROWS = [
    "Pago con tarjeta,Actual,2026-09-01 10:00:00,2026-09-02 09:00:00,Mercadona,-25.50,0.00,EUR,COMPLETADO,974.50",
    "Transferencia,Actual,2026-09-03 10:00:00,2026-09-04 09:00:00,Top-up from BBVA,850.00,0.00,EUR,COMPLETADO,1824.50",
    "Cambio,Actual,2026-09-05 10:00:00,2026-09-05 10:00:01,Exchanged to MXN,-100.00,1.00,EUR,COMPLETADO,1723.50",
    "Pago con tarjeta,Actual,2026-09-06 10:00:00,,Amazon,-9.99,0.00,EUR,PENDIENTE,",
]
REAL_CSV = os.path.expanduser("~/Downloads/revoluteur.csv")


def _write(tmp_path, lines, header=ES_HEADER):
    path = tmp_path / "revolut.csv"
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return str(path)


def test_parses_completed_rows_with_net_amounts(tmp_path):
    txns = RevolutCsvParser().parse(_write(tmp_path, ROWS))
    assert [t.amount for t in txns] == [Decimal("-25.50"), Decimal("850.00"), Decimal("-101.00")]
    assert [t.date for t in txns] == [date(2026, 9, 2), date(2026, 9, 4), date(2026, 9, 5)]
    assert {t.source_account for t in txns} == {"Revolut EUR"}
    assert txns[0].raw_payee == "Mercadona"
    assert txns[0].notes == "Pago con tarjeta"


def test_statement_meta_from_balance_chain(tmp_path):
    parsed = RevolutCsvParser().parse_statement(_write(tmp_path, ROWS))
    assert parsed.meta.opening_balance == Decimal("1000.00")
    assert parsed.meta.ending_balance == Decimal("1723.50")
    assert parsed.period() == (date(2026, 9, 2), date(2026, 9, 5))


def test_english_headers(tmp_path):
    rows = [r.replace("COMPLETADO", "COMPLETED").replace("PENDIENTE", "PENDING") for r in ROWS]
    assert len(RevolutCsvParser().parse(_write(tmp_path, rows, EN_HEADER))) == 3


def test_broken_balance_chain_fails(tmp_path):
    rows = [ROWS[0], ROWS[1].replace("1824.50", "1824.00"), ROWS[2]]
    with pytest.raises(ParseValidationError, match="cadena de saldos"):
        RevolutCsvParser().parse(_write(tmp_path, rows))


def test_mixed_currencies_fail(tmp_path):
    rows = [ROWS[0], ROWS[1].replace(",EUR,", ",MXN,")]
    with pytest.raises(ParseValidationError, match="mezcla divisas"):
        RevolutCsvParser().parse(_write(tmp_path, rows))


def test_detection_and_registry():
    det = detect_csv_text("\n".join([ES_HEADER, *ROWS]))
    assert (det.source_bank, det.format, det.parser_available) == ("revolut", "csv", True)
    assert isinstance(get_parser("revolut", "csv"), RevolutCsvParser)


@pytest.mark.skipif(not os.path.exists(REAL_CSV), reason="CSV real de Revolut no disponible")
def test_real_export_balances():
    parsed = RevolutCsvParser().parse_statement(REAL_CSV)
    with open(REAL_CSV, encoding="utf-8-sig", newline="") as f:
        last = list(csv.DictReader(f))[-1]
    assert parsed.meta.ending_balance == Decimal(last["Saldo"])
    assert parsed.meta.opening_balance + sum(t.amount for t in parsed.transactions) == parsed.meta.ending_balance
```

- [ ] **Step 2: Ejecutar el test para verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_revolut.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'spend_pipe.parsers.revolut'`.

- [ ] **Step 3: Implementar el parser**

`spend_pipe/parsers/revolut.py`:

```python
"""Revolut — extracto CSV de la app (cabeceras en español o en inglés).

Solo entran filas completadas: las pendientes o revertidas no afectan al saldo.
El importe neto es Importe − Comisión, y el candado es la cadena de saldos:
cada Saldo debe ser el anterior más el importe neto. Si no cuadra, se falla fuerte.
"""
from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation

from ..schema import CommonTransaction
from ..statement import ParsedFile, StatementMeta
from .errors import LayoutNotRecognizedError, ParseValidationError

COLUMNS: dict[str, dict[str, str]] = {
    "es": {
        "type": "Tipo", "completed": "Fecha de finalización", "description": "Descripción",
        "amount": "Importe", "fee": "Comisión", "currency": "Divisa", "state": "Estado", "balance": "Saldo",
    },
    "en": {
        "type": "Type", "completed": "Completed Date", "description": "Description",
        "amount": "Amount", "fee": "Fee", "currency": "Currency", "state": "State", "balance": "Balance",
    },
}
COMPLETED_STATES = {"COMPLETADO", "COMPLETED"}


def revolut_columns(headers) -> dict[str, str] | None:
    present = {(h or "").strip() for h in headers}
    for cols in COLUMNS.values():
        if set(cols.values()) <= present:
            return cols
    return None


def _decimal(value: str | None, field: str, line: int) -> Decimal:
    try:
        return Decimal((value or "").strip() or "0")
    except InvalidOperation:
        raise ParseValidationError(f"Revolut: {field} inválido en la línea {line}: {value!r}") from None


class RevolutCsvParser:
    source_bank = "revolut"
    format = "csv"
    source_account = "Revolut"  # la cuenta real es 'Revolut <divisa>' y se fija por fila

    def parse(self, file_path: str) -> list[CommonTransaction]:
        return self.parse_statement(file_path).transactions

    def parse_statement(self, file_path: str) -> ParsedFile:
        with open(file_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cols = revolut_columns(reader.fieldnames or [])
            if cols is None:
                raise LayoutNotRecognizedError("CSV de Revolut no reconocido: faltan columnas esperadas.")
            rows = [
                (i + 2, row)
                for i, row in enumerate(reader)
                if (row.get(cols["state"]) or "").strip().upper() in COMPLETED_STATES
            ]

        currencies = {row[cols["currency"]].strip() for _, row in rows}
        if len(currencies) > 1:
            raise ParseValidationError(f"Revolut: el CSV mezcla divisas {sorted(currencies)}; exporta una cuenta por archivo.")

        rows.sort(key=lambda item: item[1][cols["completed"]])  # sort estable: respeta el orden del archivo en empates
        txns: list[CommonTransaction] = []
        opening: Decimal | None = None
        previous: Decimal | None = None
        for index, (line, row) in enumerate(rows):
            net = _decimal(row[cols["amount"]], "importe", line) - _decimal(row[cols["fee"]], "comisión", line)
            balance = _decimal(row[cols["balance"]], "saldo", line)
            if previous is None:
                opening = balance - net
            elif previous + net != balance:
                raise ParseValidationError(
                    f"Revolut: la cadena de saldos no cuadra en la línea {line}: {previous} + {net} ≠ {balance}"
                )
            previous = balance
            currency = row[cols["currency"]].strip()
            txns.append(
                CommonTransaction(
                    source_bank=self.source_bank,
                    source_account=f"Revolut {currency}",
                    format=self.format,
                    date=date.fromisoformat(row[cols["completed"]].strip()[:10]),
                    amount=net,
                    currency=currency,
                    raw_payee=row[cols["description"]].strip(),
                    notes=(row.get(cols["type"]) or "").strip() or None,
                    source_file=file_path.rsplit("/", 1)[-1],
                    source_row=index,
                )
            )

        if not txns:
            return ParsedFile([])
        dates = [t.date for t in txns]
        return ParsedFile(
            txns,
            StatementMeta(period_from=min(dates), period_to=max(dates), opening_balance=opening, ending_balance=previous),
        )
```

- [ ] **Step 4: Registrar y detectar**

Al final de `spend_pipe/parsers/configured.py`:

```python
# Revolut — CSV de la app (es/en). Una divisa por archivo; la cuenta es 'Revolut <divisa>'.
from .revolut import RevolutCsvParser  # noqa: E402

register(RevolutCsvParser())
```

En `spend_pipe/parsers/sniff.py`, añadir el import junto a los demás:

```python
from .revolut import revolut_columns
```

y, en `detect_csv_text`, justo después de la línea `# ── Fingerprints exactos de formatos conocidos ──`, insertar:

```python
    if revolut_columns(headers):
        return Detection(
            kind="csv", source_bank="revolut", format="csv", source_account="Revolut",
            parser_available=True, confidence="high", label="Revolut — extracto CSV",
            csv_headers=headers, csv_delimiter=delimiter, csv_header_row=header_row,
        )
```

- [ ] **Step 5: Ejecutar los tests**

Run: `.venv/bin/python -m pytest -q`
Expected: todos PASS, incluido `test_real_export_balances` si `~/Downloads/revoluteur.csv` existe.

- [ ] **Step 6: Commit**

```bash
git add spend_pipe/parsers/revolut.py spend_pipe/parsers/configured.py spend_pipe/parsers/sniff.py tests/test_revolut.py
git commit -m "feat(revolut): CSV parser with net amounts and balance-chain check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Endpoint `POST /api/parse` y app mínima

**Files:**
- Create: `spend_pipe/api_parse.py`, `spend_pipe/parse_app.py`
- Modify: `README.md`
- Test: `tests/test_api_parse.py`

**Interfaces:**
- Consumes: `parse_statement`, `detect_file`, `get_parser`, `assign_imported_ids`, `amount_to_cents`.
- Produces: `PARSE_SCHEMA_VERSION = "parse-1"`, `parse_file(path) -> tuple[int, dict]`, `router` (FastAPI) con `POST /api/parse`, `parse_app.app` con `/api/parse` y `GET /health`. Respuesta 200:

```json
{
  "schemaVersion": "parse-1",
  "institution": "revolut",
  "accountHint": "Revolut EUR",
  "currency": "EUR",
  "period": { "from": "2026-09-02", "to": "2026-09-05" },
  "openingBalance": 100000,
  "endingBalance": 172350,
  "rows": [{ "date": "2026-09-02", "amount": -2550, "payee": "Mercadona", "memo": "Pago con tarjeta", "importedId": "spendpipe:revolut-eur:…", "pending": false }]
}
```

Errores 422: `{"error": "unsupported", "label", "guidance"}`, `{"error": "parse_failed", "message"}`, `{"error": "empty", "message"}`. 401 si falta el token configurado.

- [ ] **Step 1: Escribir el test que falla**

`tests/test_api_parse.py`:

```python
from fastapi.testclient import TestClient

from spend_pipe.identity import assign_imported_ids
from spend_pipe.parse_app import app

ES_HEADER = "Tipo,Producto,Fecha de inicio,Fecha de finalización,Descripción,Importe,Comisión,Divisa,Estado,Saldo"
ROWS = [
    "Pago con tarjeta,Actual,2026-09-01 10:00:00,2026-09-02 09:00:00,Mercadona,-25.50,0.00,EUR,COMPLETADO,974.50",
    "Transferencia,Actual,2026-09-03 10:00:00,2026-09-04 09:00:00,Top-up from BBVA,850.00,0.00,EUR,COMPLETADO,1824.50",
    "Cambio,Actual,2026-09-05 10:00:00,2026-09-05 10:00:01,Exchanged to MXN,-100.00,1.00,EUR,COMPLETADO,1723.50",
]
REVOLUT = ("\n".join([ES_HEADER, *ROWS]) + "\n").encode("utf-8")
client = TestClient(app)


def _post(content: bytes, name: str = "rev.csv", headers: dict | None = None):
    return client.post("/api/parse", files={"file": (name, content, "text/csv")}, headers=headers or {})


def test_parses_a_revolut_statement(monkeypatch):
    monkeypatch.delenv("SPENDPIPE_PARSE_TOKEN", raising=False)
    res = _post(REVOLUT)
    assert res.status_code == 200
    body = res.json()
    assert body["schemaVersion"] == "parse-1"
    assert (body["institution"], body["accountHint"], body["currency"]) == ("revolut", "Revolut EUR", "EUR")
    assert body["period"] == {"from": "2026-09-02", "to": "2026-09-05"}
    assert (body["openingBalance"], body["endingBalance"]) == (100000, 172350)
    assert [r["amount"] for r in body["rows"]] == [-2550, 85000, -10100]
    assert body["rows"][0]["memo"] == "Pago con tarjeta"
    assert body["rows"][0]["pending"] is False


def test_imported_ids_match_the_staging_formula(monkeypatch):
    monkeypatch.delenv("SPENDPIPE_PARSE_TOKEN", raising=False)
    rows = _post(REVOLUT).json()["rows"]
    expected = assign_imported_ids([("Revolut EUR", r["date"], r["amount"] / 100, r["payee"]) for r in rows])
    assert [r["importedId"] for r in rows] == expected
    assert rows[0]["importedId"].startswith("spendpipe:revolut-eur:2026-09-02:-2550:")


def test_unsupported_file_is_422(monkeypatch):
    monkeypatch.delenv("SPENDPIPE_PARSE_TOKEN", raising=False)
    res = _post(b"hola, esto no es un extracto", name="nota.txt")
    assert res.status_code == 422
    assert res.json()["error"] == "unsupported"


def test_parse_failure_is_422(monkeypatch):
    monkeypatch.delenv("SPENDPIPE_PARSE_TOKEN", raising=False)
    broken = REVOLUT.replace(b"1824.50", b"1824.00")
    res = _post(broken)
    assert res.status_code == 422
    assert res.json()["error"] == "parse_failed"
    assert "cadena de saldos" in res.json()["message"]


def test_token_is_required_when_configured(monkeypatch):
    monkeypatch.setenv("SPENDPIPE_PARSE_TOKEN", "s3cret")
    assert _post(REVOLUT).status_code == 401
    assert _post(REVOLUT, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert _post(REVOLUT, headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_health():
    assert client.get("/health").json() == {"ok": True}
```

- [ ] **Step 2: Ejecutar el test para verificar que falla**

Run: `.venv/bin/python -m pytest tests/test_api_parse.py -q`
Expected: FAIL con `ModuleNotFoundError: No module named 'spend_pipe.parse_app'`.

- [ ] **Step 3: Implementar el endpoint y la app**

`spend_pipe/api_parse.py`:

```python
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
```

`spend_pipe/parse_app.py`:

```python
"""Servicio mínimo con solo /api/parse: sin base de datos ni migraciones.

    uvicorn spend_pipe.parse_app:app --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI

from .api_parse import router

app = FastAPI(title="spend-pipe parse")
app.include_router(router)


@app.get("/health")
def health() -> dict:
    return {"ok": True}
```

- [ ] **Step 4: Documentar en el README**

Añadir al final de `README.md`:

```markdown
## Servicio de parseo para ledger-guard

`POST /api/parse` recibe un extracto (multipart, campo `file`) y devuelve filas
normalizadas con `imported_id`, periodo y saldos en céntimos, sin tocar staging.
Se sirve con una app mínima sin base de datos:

    uvicorn spend_pipe.parse_app:app --host 0.0.0.0 --port 8000

Si `SPENDPIPE_PARSE_TOKEN` está definido, exige `Authorization: Bearer <token>`.
Soporta los formatos con parser registrado: BBVA (PDF de cuenta y de tarjeta, CSV
de tarjeta), Openbank (.xls de cuenta y de tarjeta) y Revolut (CSV).
```

- [ ] **Step 5: Ejecutar toda la batería**

Run: `.venv/bin/python -m pytest -q`
Expected: todos PASS.

- [ ] **Step 6: Commit**

```bash
git add spend_pipe/api_parse.py spend_pipe/parse_app.py tests/test_api_parse.py README.md
git commit -m "feat(api): stateless POST /api/parse for ledger-guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
