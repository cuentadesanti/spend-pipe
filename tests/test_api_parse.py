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
