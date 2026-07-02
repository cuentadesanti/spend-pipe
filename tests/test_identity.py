"""Tests de las propiedades que hacen confiable la identidad. Solo stdlib."""
from spend_pipe.identity import (
    amount_to_cents,
    assign_imported_ids,
    dedup_hash,
    imported_id,
)


def test_amount_cents_no_drift():
    # '532.0' y '532.00' deben dar la misma identidad.
    assert amount_to_cents("-532.0") == amount_to_cents("-532.00") == -53200


def test_payee_canonicalization_is_stable():
    # Diferencias de mayúsculas/espacios en el payee crudo no cambian la identidad.
    a = imported_id("bbva-tdc", "2026-05-05", "-532.00", "UBER  RIDE", 0)
    b = imported_id("bbva-tdc", "2026-05-05", "-532.00", "uber ride", 0)
    assert a == b


def test_same_day_duplicates_get_distinct_occurrences():
    # Dos cafés iguales el mismo día NO se colapsan: occ 0 y 1.
    rows = [
        ("bbva-tdc", "2026-05-05", "-70.00", "STARBUCKS", ),
        ("bbva-tdc", "2026-05-05", "-70.00", "STARBUCKS", ),
    ]
    # assign_imported_ids espera (account, date, amount, raw_payee)
    ids = assign_imported_ids([(r[0], r[1], r[2], r[3]) for r in rows])
    assert ids[0].endswith(":0")
    assert ids[1].endswith(":1")
    assert ids[0] != ids[1]


def test_imported_id_independent_of_normalization():
    # Mejorar la normalización (payee mostrado) NO debe correr la identidad hacia Actual,
    # porque imported_id se construye con el payee CRUDO, no el normalizado.
    raw = "UBER TRIP HELP.UBER.COM"
    before = imported_id("bbva-tdc", "2026-05-26", "-70.00", raw, 0)
    after = imported_id("bbva-tdc", "2026-05-26", "-70.00", raw, 0)
    assert before == after  # el raw no cambió aunque el normalizado pasara de 'Uber trip' a 'Uber'


def test_dedup_hash_uses_normalized_payee():
    # Dos fuentes escriben el comercio distinto en crudo pero igual normalizado → mismo dedup_hash.
    h1 = dedup_hash("BBVA TDC", "2026-05-26", "-70.00", "Uber")
    h2 = dedup_hash("BBVA TDC", "2026-05-26", "-70.00", "uber")
    assert h1 == h2


def test_dedup_hash_differs_from_imported_id():
    # Claves distintas para trabajos distintos.
    iid = imported_id("bbva-tdc", "2026-05-26", "-70.00", "UBER RIDE", 0)
    dh = dedup_hash("bbva-tdc", "2026-05-26", "-70.00", "Uber")
    assert iid != dh
