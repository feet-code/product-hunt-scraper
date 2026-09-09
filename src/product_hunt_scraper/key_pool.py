from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Iterable

from .batch import Ledger
from .http import HttpError, PoliteHttpClient


LOG = logging.getLogger(__name__)


def _fingerprint(api_key: str) -> str:
    # Stable internal identifier so persisted quota state survives key reordering.
    # The raw credential is never written to SQLite or logs.
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


class GeminiKeyPoolLedger:
    """Expose the Ledger interface while accounting independently per API key."""

    def __init__(self, base_ledger: Ledger, api_keys: Iterable[str]) -> None:
        self.base_ledger = base_ledger
        self.api_keys = tuple(dict.fromkeys(key.strip() for key in api_keys if key.strip()))
        if not self.api_keys:
            raise ValueError(
                "Set GEMINI_API_KEY or GEMINI_API_KEYS before generation."
            )

        database_row = base_ledger.db.execute("PRAGMA database_list").fetchone()
        database_path = Path(database_row["file"])
        if not str(database_path):
            raise ValueError("Gemini key pooling requires a file-backed quota ledger.")

        self.ledgers: list[Ledger] = []
        for api_key in self.api_keys:
            ledger = Ledger(
                database_path,
                scope=f"{base_ledger.scope}:key:{_fingerprint(api_key)}",
                clock=base_ledger.clock,
            )
            ledger.rpd = base_ledger.rpd
            ledger.rpm = base_ledger.rpm
            ledger.tpm = base_ledger.tpm
            self.ledgers.append(ledger)

        self.clock = base_ledger.clock
        self.tpm = base_ledger.tpm
        cursor_key = f"gemini-key-pool-cursor:{base_ledger.scope}"
        self._cursor_key = cursor_key
        try:
            self._cursor = int(base_ledger.get(cursor_key, "0")) % len(self.api_keys)
        except ValueError:
            self._cursor = 0
        self._active_index: int | None = None

    @property
    def active_api_key(self) -> str:
        if self._active_index is None:
            raise RuntimeError("No Gemini key has been claimed for the current request.")
        return self.api_keys[self._active_index]

    @property
    def active_label(self) -> str:
        if self._active_index is None:
            return "key-none"
        return f"key-{self._active_index + 1}"

    def claim(self, model: str, tokens: int) -> bool:
        count = len(self.ledgers)
        for offset in range(count):
            index = (self._cursor + offset) % count
            if self.ledgers[index].claim(model, tokens):
                self._active_index = index
                self._cursor = (index + 1) % count
                self.base_ledger.set(self._cursor_key, self._cursor)
                LOG.info(
                    "Gemini pool selected %s for %s (%d keys configured)",
                    self.active_label,
                    model,
                    count,
                )
                return True
        self._active_index = None
        return False

    def ready_at(self, model: str, tokens: int = 0) -> float:
        return min(ledger.ready_at(model, tokens) for ledger in self.ledgers)

    def record_outcome(self, model: str, kind: str) -> None:
        if self._active_index is None:
            return
        self.ledgers[self._active_index].record_outcome(model, kind)

    def block(self, model: str, error: HttpError) -> None:
        if self._active_index is None:
            return
        active = self._active_index
        self.ledgers[active].block(model, error)
        # A 503 is model/service availability, not a key quota signal. Block the
        # same model across the pool so BatchGenerator falls back to another model
        # instead of burning one identical request per collaborator key.
        if error.status == 503:
            for index, ledger in enumerate(self.ledgers):
                if index != active:
                    ledger.block(model, error)

    def summary(self, models: Iterable[str]) -> dict[str, object]:
        models = tuple(models)
        return {
            f"key-{index + 1}": ledger.summary(models)
            for index, ledger in enumerate(self.ledgers)
        }

    def close(self) -> None:
        for ledger in self.ledgers:
            ledger.db.close()


class PooledGeminiClient:
    """Inject the key selected by GeminiKeyPoolLedger into each Gemini request."""

    def __init__(self, client: PoliteHttpClient, pool: GeminiKeyPoolLedger) -> None:
        self._client = client
        self._pool = pool

    @property
    def max_attempts(self) -> int:
        return self._client.max_attempts

    @max_attempts.setter
    def max_attempts(self, value: int) -> None:
        self._client.max_attempts = value

    def post_json(self, url: str, payload: object, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["x-goog-api-key"] = self._pool.active_api_key
        return self._client.post_json(url, payload, headers=headers, **kwargs)
