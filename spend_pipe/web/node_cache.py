"""Cache stale-while-revalidate para las listas que vienen de Actual vía Node.

La cura del 499 (timeout de 15s del proxy): descargar el budget de PikaPods tarda
10-60s, así que NINGUNA request puede bloquearse en el subprocess de Node. Regla:

  - una request SIEMPRE responde al instante con lo que haya (memoria → archivo →
    fallback), aunque esté vencido;
  - si está vencido o vacío, dispara UN refresh en un hilo de fondo (con lock:
    nunca dos subprocess a la vez) y la respuesta fresca la ve la próxima request;
  - el startup pre-calienta en background sin bloquear el arranque.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path


class NodeListCache:
    def __init__(
        self,
        script: str,                 # ej. 'get_categories.js'
        cache_file: Path,
        node_dir: Path,
        ttl_seconds: int = 14400,    # 4 horas
        timeout: int = 120,          # el subprocess corre en background: puede ser generoso
        fallback: list | None = None,
    ):
        self.script = script
        self.cache_file = cache_file
        self.node_dir = node_dir
        self.ttl = ttl_seconds
        self.timeout = timeout
        self.fallback = fallback or []
        self._data: list = []
        self._fetched_at: float = 0.0
        self._refreshing = threading.Lock()

    # ── API pública ──────────────────────────────────────────────────────────
    def get(self) -> list:
        """Nunca bloquea: devuelve lo mejor disponible y refresca en background."""
        if not self._data:
            self._load_from_file()
        if self._is_stale():
            self.refresh_in_background()
        return self._data or self.fallback

    def refresh_in_background(self) -> None:
        if self._refreshing.locked():
            return  # ya hay un refresh en curso
        threading.Thread(target=self._refresh, daemon=True).start()

    # ── Internals ────────────────────────────────────────────────────────────
    def _is_stale(self) -> bool:
        return not self._data or (time.time() - self._fetched_at) > self.ttl

    def _load_from_file(self) -> None:
        try:
            if self.cache_file.exists():
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if data:
                    self._data = data
                    self._fetched_at = self.cache_file.stat().st_mtime
        except Exception as e:  # noqa: BLE001
            print(f"[node_cache] error leyendo {self.cache_file.name}: {e}")

    def _refresh(self) -> None:
        if not self._refreshing.acquire(blocking=False):
            return
        try:
            proc = subprocess.run(
                ["node", self.script],
                cwd=str(self.node_dir), capture_output=True, text=True, timeout=self.timeout,
            )
            if proc.returncode != 0:
                print(f"[node_cache] {self.script} falló: {proc.stderr[-300:]}")
                return
            line = next((l for l in proc.stdout.splitlines() if l.strip().startswith("[")), None)
            data = json.loads(line) if line else None
            if data:
                self._data = data
                self._fetched_at = time.time()
                try:
                    self.cache_file.write_text(json.dumps(data), encoding="utf-8")
                except Exception as we:  # noqa: BLE001
                    print(f"[node_cache] error escribiendo cache: {we}")
        except Exception as e:  # noqa: BLE001
            print(f"[node_cache] error refrescando {self.script}: {e}")
        finally:
            self._refreshing.release()
