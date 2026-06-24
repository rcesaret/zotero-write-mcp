"""Write gateway v2 — the single mutation chokepoint over the Zotero Web API v3.

This is the hardened write path (PRD TC-GW; DR4). Every Zotero mutation flows through it; higher
layers (the tools, the merge engine) never call the raw API. Phase-0 concerns realized here:

* **Structured returns** (P0-structured-returns). A multi-object write returns HTTP 200 with a body
  partitioning results into ``successful`` / ``unchanged`` / ``failed`` keyed by array index — 200
  means "inspect the body." Parsed into a :class:`WriteResult` (``item_keys``, per-index maps, the new
  ``last_modified_version``, and ``failed_objects()`` to resubmit only the failures).
* **Versioning / optimistic concurrency** (P0-versioning). Every mutating call carries
  ``If-Unmodified-Since-Version``; a version-less write on an existing object is **rejected** (it would
  428). On 412, ``update_item`` re-reads the current version and retries once; ``delete_items`` surfaces
  :class:`ConcurrencyConflictError` so the caller re-resolves / routes to rollback.
* **Batch + rate discipline** (P0-batch-concurrency). ``*_chunked`` split >50 objects into ≤50-object
  requests and merge the results (re-indexed to the global object positions); a rate governor honors
  ``Backoff`` (pause all new requests) and ``Retry-After`` (429 → sleep + retry). The ``≤4`` concurrency
  ceiling is respected trivially (requests are sequential); a bounded pool can add real concurrency later.
* **PUT guard** (P0-put-guard). PUT silently drops omitted fields, so ``replace_item`` refuses unless an
  explicit ``complete_object=True`` flag is set, and re-GETs the fresh version immediately before. Default
  every mutation to PATCH (``update_item``).
* **Partial-failure retry** (P0-partial-retry). ``resubmit_failed`` resubmits *only* the objects that
  failed (``failed[i]``), never the whole batch.

The gateway depends only on a small *transport* duck-type (``request(method, path, *, json, headers,
params) -> resp``-like with ``.status_code``, ``.json()``, ``.headers``), plus injectable ``sleep`` /
``monotonic`` clocks — so all logic is unit-testable with a fake transport (no network, no live library).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Pinned constants (DR4). The "10" that circulated was an Airtable error misattributed to Zotero;
# 50 is correct for create/update/delete — never silently downgrade. 4 is the documented concurrency
# ceiling (requests here are sequential, so it is respected trivially).
BATCH_LIMIT = 50
MAX_CONCURRENCY = 4


class GatewayError(Exception):
    """A write could not be completed safely through the gateway."""


class VersionMissingError(GatewayError):
    """A mutating write on an existing object was attempted without a version.

    Rejected at the gateway because the Web API would return 428 Precondition Required, and an
    unversioned write defeats optimistic concurrency (the sole-mutation-authority enforcement).
    """


class ConcurrencyConflictError(GatewayError):
    """A 412 Precondition Failed could not be resolved by the gateway.

    The library changed under the operation; the caller must re-resolve / re-snapshot (and, for a
    merge, route to rollback) rather than overwrite a concurrent change.
    """


def _header_int(headers: Any, name: str) -> Optional[int]:
    """Read an integer-valued response header (case-insensitive)."""
    if headers is None:
        return None
    try:
        v = headers.get(name)
        if v is None:
            v = headers.get(name.lower())
    except AttributeError:
        return None
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_int_version(headers: Any) -> Optional[int]:
    """Read ``Last-Modified-Version`` from response headers as an int."""
    return _header_int(headers, "Last-Modified-Version")


@dataclass
class WriteResult:
    """Structured outcome of a gateway write.

    For a multi-object create the per-index maps mirror the Web API envelope (re-indexed to global
    object positions when chunked). For a single-object update/delete (HTTP 204) the maps are empty but
    ``item_keys`` and ``last_modified_version`` are set.
    """

    status_code: int
    item_keys: list[str] = field(default_factory=list)
    successful: dict = field(default_factory=dict)   # index(str) -> full object
    unchanged: dict = field(default_factory=dict)     # index(str) -> key
    failed: dict = field(default_factory=dict)        # index(str) -> {key, code, message}
    last_modified_version: Optional[int] = None

    @property
    def all_ok(self) -> bool:
        return not self.failed

    @property
    def failed_indices(self) -> list[int]:
        return sorted(int(i) for i in self.failed)

    def failed_objects(self, objects: list[dict]) -> list[dict]:
        """Given the originally-submitted objects, return only those that failed (for retry)."""
        return [objects[i] for i in self.failed_indices if 0 <= i < len(objects)]


def parse_write_envelope(body: Optional[dict], headers: Any = None) -> WriteResult:
    """Parse a multi-object Web API write response (HTTP 200) into a :class:`WriteResult`."""
    body = body or {}
    successful = body.get("successful", {}) or {}
    success_keys = body.get("success", {}) or {}
    unchanged = body.get("unchanged", {}) or {}
    failed = body.get("failed", {}) or {}

    item_keys: list[str] = []
    if success_keys:
        for idx in sorted(success_keys, key=lambda i: int(i)):
            item_keys.append(success_keys[idx])
    elif successful:
        for idx in sorted(successful, key=lambda i: int(i)):
            obj = successful[idx]
            key = obj.get("key") if isinstance(obj, dict) else None
            if key:
                item_keys.append(key)

    return WriteResult(
        status_code=200,
        item_keys=item_keys,
        successful=successful,
        unchanged=unchanged,
        failed=failed,
        last_modified_version=_to_int_version(headers),
    )


class WriteGateway:
    """Structured, versioned, rate-disciplined writes over the Zotero Web API v3.

    Args:
        transport: duck-typed transport with ``request(method, path, *, json=None, headers=None,
            params=None) -> resp`` (``.status_code``, ``.json()``, ``.headers``).
        batch_limit: per-request object ceiling (pinned 50).
        sleep / monotonic: injectable clocks (tests pass fakes to avoid real waiting).
        max_429_retries: how many times a single request retries on 429 before giving up.
    """

    def __init__(
        self,
        transport: Any,
        *,
        batch_limit: int = BATCH_LIMIT,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        max_429_retries: int = 3,
    ):
        self._t = transport
        self.batch_limit = batch_limit
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._max_429_retries = max_429_retries
        self._resume_at = 0.0  # monotonic time before which no new request may be sent (from Backoff)

    # ── rate governor (Backoff / Retry-After) ───────────────────────────────────
    def _request(self, method: str, path: str, **kw):
        """Transport call wrapped with backpressure: wait out any pending ``Backoff``, capture a new
        one from the response, and retry on 429 honoring ``Retry-After``."""
        now = self._monotonic()
        if now < self._resume_at:
            self._sleep(self._resume_at - now)
        attempts = 0
        while True:
            resp = self._t.request(method, path, **kw)
            backoff = _header_int(getattr(resp, "headers", None), "Backoff")
            if backoff:
                self._resume_at = self._monotonic() + backoff
            if resp.status_code == 429 and attempts < self._max_429_retries:
                attempts += 1
                self._sleep(_header_int(resp.headers, "Retry-After") or 1)
                continue
            return resp

    # ── create (POST array) ─────────────────────────────────────────────────────
    def create_items(self, library_id: int, objects: list[dict]) -> WriteResult:
        """POST a single batch (≤ ``batch_limit``) of new items; return the structured envelope.

        Use :meth:`create_items_chunked` for arbitrary sizes."""
        if len(objects) > self.batch_limit:
            raise GatewayError(
                f"create batch of {len(objects)} exceeds the {self.batch_limit}-object limit; "
                "use create_items_chunked()")
        resp = self._request(
            "POST", f"/users/{library_id}/items",
            json=objects, headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            raise GatewayError(f"create_items: unexpected HTTP {resp.status_code}")
        return parse_write_envelope(resp.json(), resp.headers)

    def create_items_chunked(self, library_id: int, objects: list[dict]) -> WriteResult:
        """Split >50 objects into ≤50-object POSTs and merge, re-indexing the per-index maps to the
        global object positions so ``failed_objects(objects)`` works against the full input list."""
        merged = WriteResult(status_code=200)
        for start in range(0, len(objects), self.batch_limit):
            chunk = objects[start:start + self.batch_limit]
            self._merge_into(merged, self.create_items(library_id, chunk), start)
        return merged

    @staticmethod
    def _merge_into(merged: WriteResult, res: WriteResult, offset: int) -> None:
        merged.item_keys.extend(res.item_keys)
        for k, v in res.successful.items():
            merged.successful[str(int(k) + offset)] = v
        for k, v in res.unchanged.items():
            merged.unchanged[str(int(k) + offset)] = v
        for k, v in res.failed.items():
            merged.failed[str(int(k) + offset)] = v
        if res.last_modified_version is not None:
            merged.last_modified_version = res.last_modified_version

    def resubmit_failed(self, library_id: int, objects: list[dict], result: WriteResult) -> WriteResult:
        """Resubmit ONLY the objects that failed in ``result`` (the ``failed[i]`` subset), never the
        whole batch. Returns the result of the resubmission (the caller merges/decides)."""
        failed = result.failed_objects(objects)
        if not failed:
            return WriteResult(status_code=200)
        return self.create_items_chunked(library_id, failed)

    # ── update (single PATCH, versioned, 412 re-GET + retry once) ───────────────
    def update_item(self, library_id: int, item_key: str, data: dict,
                    version: Optional[int]) -> WriteResult:
        """PATCH a single item. Requires ``version``; retries once on 412 with the fresh version.

        Default to PATCH (omitted scalars untouched). Array properties (collections/tags/creators/
        relations) are complete-list replaces — callers pre-compute the union; the gateway does not
        second-guess the supplied ``data``."""
        if version is None:
            raise VersionMissingError(
                f"update_item({item_key}) requires a version (If-Unmodified-Since-Version); "
                "version-less writes are rejected to prevent a 428.")
        resp = self._patch_once(library_id, item_key, data, version)
        if resp.status_code == 412:
            fresh = self._current_item_version(library_id, item_key)
            resp = self._patch_once(library_id, item_key, data, fresh)
            if resp.status_code == 412:
                raise ConcurrencyConflictError(
                    f"update_item({item_key}) still 412 after re-GET; re-resolve / re-snapshot required.")
        if resp.status_code not in (200, 204):
            raise GatewayError(f"update_item({item_key}): unexpected HTTP {resp.status_code}")
        return WriteResult(status_code=resp.status_code, item_keys=[item_key],
                           last_modified_version=_to_int_version(resp.headers))

    def _patch_once(self, library_id: int, item_key: str, data: dict, version: int):
        return self._request(
            "PATCH", f"/users/{library_id}/items/{item_key}", json=data,
            headers={"Content-Type": "application/json",
                     "If-Unmodified-Since-Version": str(version)})

    def _current_item_version(self, library_id: int, item_key: str) -> int:
        resp = self._request("GET", f"/users/{library_id}/items/{item_key}")
        body = resp.json() or {}
        v = body.get("version")
        if v is None:
            v = (body.get("data") or {}).get("version")
        if v is None:
            raise GatewayError(f"could not read current version for {item_key}")
        return int(v)

    # ── replace (PUT) — forbidden except behind an explicit complete-object flag ─
    def replace_item(self, library_id: int, item_key: str, full_data: dict,
                     *, complete_object: bool = False) -> WriteResult:
        """Full-replace an item via PUT — guarded. PUT silently drops omitted fields, so this refuses
        unless ``complete_object=True`` and re-GETs the fresh version immediately before writing. Prefer
        :meth:`update_item` (PATCH) for everything else."""
        if not complete_object:
            raise GatewayError(
                "PUT (full replace) is forbidden unless complete_object=True — PUT silently drops "
                "omitted fields. Default to PATCH via update_item().")
        fresh = self._current_item_version(library_id, item_key)  # immediate re-GET (DR4 guard)
        resp = self._request(
            "PUT", f"/users/{library_id}/items/{item_key}", json=full_data,
            headers={"Content-Type": "application/json", "If-Unmodified-Since-Version": str(fresh)})
        if resp.status_code == 412:
            raise ConcurrencyConflictError(f"replace_item({item_key}): 412 on PUT after re-GET.")
        if resp.status_code not in (200, 204):
            raise GatewayError(f"replace_item({item_key}): unexpected HTTP {resp.status_code}")
        return WriteResult(status_code=resp.status_code, item_keys=[item_key],
                           last_modified_version=_to_int_version(resp.headers))

    # ── delete (versioned; 412 → caller re-resolves) ────────────────────────────
    def delete_items(self, library_id: int, item_keys: list[str],
                     version: Optional[int]) -> WriteResult:
        """DELETE (trash) up to 50 items, gated by the library version. 412 → ConcurrencyConflictError
        (no blind re-delete). Trashes, never purges. Use :meth:`delete_items_chunked` for >50."""
        if version is None:
            raise VersionMissingError(
                "delete_items requires the library version; version-less deletes are rejected.")
        if len(item_keys) > self.batch_limit:
            raise GatewayError(
                f"delete batch of {len(item_keys)} exceeds the {self.batch_limit}-object limit; "
                "use delete_items_chunked()")
        resp = self._request(
            "DELETE", f"/users/{library_id}/items",
            params={"itemKey": ",".join(item_keys)},
            headers={"If-Unmodified-Since-Version": str(version)})
        if resp.status_code == 412:
            raise ConcurrencyConflictError(
                "delete_items: 412 (library changed); caller must re-resolve / route to rollback.")
        if resp.status_code not in (200, 204):
            raise GatewayError(f"delete_items: unexpected HTTP {resp.status_code}")
        return WriteResult(status_code=resp.status_code, item_keys=list(item_keys),
                           last_modified_version=_to_int_version(resp.headers))

    def delete_items_chunked(self, library_id: int, item_keys: list[str],
                             version: int) -> WriteResult:
        """Split >50 deletes into ≤50-key requests, threading the library version forward (each
        successful delete advances ``Last-Modified-Version``)."""
        merged = WriteResult(status_code=204, last_modified_version=version)
        cur = version
        for start in range(0, len(item_keys), self.batch_limit):
            chunk = item_keys[start:start + self.batch_limit]
            res = self.delete_items(library_id, chunk, cur)
            merged.item_keys.extend(res.item_keys)
            if res.last_modified_version is not None:
                merged.last_modified_version = res.last_modified_version
                cur = res.last_modified_version
        return merged
