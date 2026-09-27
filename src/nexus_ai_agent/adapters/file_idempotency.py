"""File-backed durable idempotency store — reference implementation.

This is the durable counterpart to :class:`InMemoryIdempotencyStore`
in :mod:`nexus_ai_agent.creative.studio.idempotency`. It lives in
``adapters`` (behind the adapter boundary) because it touches the
filesystem (``os``/``pathlib``/``tempfile``), which the pure
``creative/studio`` layer must not import directly (see
``tests/architecture/test_slideshow_adapter_boundary.py``).
The bus remains agnostic: it only depends on the
:class:`IdempotencyStore` Protocol, so either backend can be injected
at composition time without changing pipeline semantics.

Durability: atomic ``.tmp → rename`` JSON, ``RLock`` + reload-on-read,
survives new ``FileIdempotencyStore(path)`` instances sharing the same
path and across process restart. Corrupted file → empty (fail-closed).
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading

from nexus_ai_agent.creative.studio.idempotency import IdempotencyKey, Reservation
from nexus_ai_agent.creative.studio.models import CommandResult


class FileIdempotencyStore:
    """File-backed durable store — reference implementation for the protocol.

    Persists reservations as JSON to ``path`` with atomic ``.tmp → rename``
    durability. The file is created lazily; an empty or missing file means
    an empty store. The store is process-local for writes (``RLock``) but
    durable across ``FileIdempotencyStore`` instances sharing the same path
    and across process restart — the contract ``InMemory`` explicitly does
    *not* satisfy.
    """

    def __init__(self, path: str | pathlib.Path) -> None:
        self._path = pathlib.Path(path)
        self._lock = threading.RLock()
        self._store: dict[IdempotencyKey, Reservation] = {}
        self._load()

    def _key_to_str(self, key: IdempotencyKey) -> str:
        return json.dumps(list(key), separators=(",", ":"), ensure_ascii=False)

    def _str_to_key(self, s: str) -> IdempotencyKey:
        parts = json.loads(s)
        return (str(parts[0]), str(parts[1]), str(parts[2]))

    def _load(self) -> None:
        if not self._path.exists():
            self._store = {}
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            if not raw.strip():
                self._store = {}
                return
            data = json.loads(raw)
            store: dict[IdempotencyKey, Reservation] = {}
            for k_str, v in data.items():
                key = self._str_to_key(k_str)
                result = None
                if v.get("result") is not None:
                    result = CommandResult.model_validate(v["result"])
                store[key] = Reservation(fingerprint=v["fingerprint"], result=result)
            self._store = store
        except Exception:
            self._store = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for key, reservation in self._store.items():
            k_str = self._key_to_str(key)
            result_dump = None
            if reservation.result is not None:
                result_dump = reservation.result.model_dump(mode="json")
            data[k_str] = {"fingerprint": reservation.fingerprint, "result": result_dump}
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=self._path.name + ".tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def get(self, key: IdempotencyKey) -> Reservation | None:
        with self._lock:
            self._load()
            return self._store.get(key)

    def put(self, key: IdempotencyKey, reservation: Reservation) -> None:
        with self._lock:
            self._load()
            self._store[key] = reservation
            self._save()

    def delete(self, key: IdempotencyKey) -> None:
        with self._lock:
            self._load()
            self._store.pop(key, None)
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._save()

    def __len__(self) -> int:  # pragma: no cover
        with self._lock:
            self._load()
            return len(self._store)

    def __contains__(self, key: object) -> bool:  # pragma: no cover
        with self._lock:
            self._load()
            return key in self._store


__all__ = ["FileIdempotencyStore"]
