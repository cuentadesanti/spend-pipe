#!/usr/bin/env python3
"""Script de prueba (dry run / integration test) para verificar la desduplicación en Actual Budget.

Calcula el imported_id determinista para una transacción dummy, estructura el lote (batch),
y ejecuta el worker node (push.js) dos veces seguidas con --commit.
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
from datetime import datetime, timezone

# Asegurar que spend_pipe esté en el path de python
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from spend_pipe.identity import imported_id


def run_test():
    # 1. Definir transacción dummy
    source_account = "BBVA Cuenta Digital"
    date_str = "2026-07-02"
    amount = -50.00  # -50.00 MXN para un cargo de Uber
    payee = "Uber"

    # 2. Calcular imported_id determinista usando la misma lógica que el pipeline real
    # imported_id(source_account, date_iso, amount, raw_payee, occ)
    iid = imported_id(source_account, date_str, amount, payee, occ=0)
    print(f"[TEST] UUID / imported_id determinista generado: {iid}")

    # Mapeo de cuenta a su nombre en Actual (según export.py)
    # BBVA Cuenta Digital -> BBVA Cuenta Digital (MXN)
    actual_name = "BBVA Cuenta Digital (MXN)"

    # 3. Crear el lote (batch) JSON de prueba
    batch_data = {
        "schema_version": "1.2",
        "batch_id": "batch_test_dedup",
        "source": "spend-pipe-test",
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": "test-script",
        "currency_default": "MXN",
        "accounts": [
            {
                "actual_account_name": actual_name,
                "source_account_name": source_account,
                "transactions": [
                    {
                        "spend_pipe_transaction_id": "txn_test_dedup_1",
                        "date": date_str,
                        "amount": amount,
                        "currency": "MXN",
                        "payee_name": payee,
                        "notes": "Prueba de desduplicación automática",
                        "imported_id": iid,
                        "cleared": True,
                        "category_name": None,
                        "transfer_to_actual_account": None,
                        "subtransactions": [],
                        "metadata": {
                            "source_file": "test-dry-run",
                            "source_row": 1,
                            "raw_payee": payee,
                        },
                    }
                ],
            }
        ],
    }

    artifact_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../artifacts/batch-test-dedup.json")
    )
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(batch_data, f, ensure_ascii=False, indent=2)

    print(f"[TEST] Lote de prueba guardado en: {artifact_path}")

    # Ruta del worker de Node
    node_pusher_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../node-pusher")
    )

    # 4. Primera ejecución (inserción)
    print("\n" + "=" * 80)
    print("EJECUCIÓN 1: Envío inicial (Debería agregar 1 nueva transacción)")
    print("=" * 80)
    proc1 = subprocess.run(
        ["node", "push.js", artifact_path, "--commit"],
        cwd=node_pusher_dir,
        capture_output=True,
        text=True,
    )
    print("STDOUT:")
    print(proc1.stdout)
    if proc1.stderr:
        print("STDERR:")
        print(proc1.stderr)

    # 5. Segunda ejecución (desduplicación)
    print("\n" + "=" * 80)
    print(
        "EJECUCIÓN 2: Segundo envío idéntico (Debería ignorar/actualizar, mostrando 0 nuevas)"
    )
    print("=" * 80)
    proc2 = subprocess.run(
        ["node", "push.js", artifact_path, "--commit"],
        cwd=node_pusher_dir,
        capture_output=True,
        text=True,
    )
    print("STDOUT:")
    print(proc2.stdout)
    if proc2.stderr:
        print("STDERR:")
        print(proc2.stderr)


if __name__ == "__main__":
    run_test()
