"""Write gateway v2 — the single mutation chokepoint over the Zotero Web API v3.

This is the hardened write path (PRD TC-GW; DR4). Every Zotero mutation flows through it; higher
layers (the tools, the merge engine) never call the raw API. Two Phase-0 concerns are realized here:

* **Structured returns** (P0-structured-returns). A multi-object write returns HTTP 200 with a body
  that partitions results into ``successful`` / ``unchanged`` / ``failed`` keyed by array index — 200
  means "inspect the body," some objects can fail while others succeed. The gateway parses that
  envelope into a :class:`WriteResult` exposing ``item_keys``, the per-index maps, and the new
  ``last_modified_version`` — so an orchestrator can consume keys and retry only the failures, instead
  of parsing Markdown prose.
* **Versioning / optimistic concurrency** (P0-versioning). Every mutating call carries
  ``If-Unmodified-Since-Version``; a version-less write is **rejected at the gateway** (it would 428).
  On a 412 (the library changed), the gateway re-reads the current version and retries once for
  idempotent field updates; for deletes it surfaces a :class:`ConcurrencyConflictError` so the caller
  (e.g. ``commit_merge``) can re-resolve / route to rollback rather than blindly re-deleting.

The gateway depends only on a small *transport* duck-type (``request(method, path, *, json, headers,
params) -> response``-like with ``.status_code``, ``.json()``, ``.headers``), so the concurrency and
envelope logic is fully unit-testable with a fake transport — no network, no live library.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Pinned constants (DR4). The "10" that circulated was an Airtable error misattributed to Zotero;
# 50 is correct for create/update/delete. Never silently downgrade. Chunking + the <=4 concurrency
# cap + Backoff/Retry-After handling land in P0-batch-concurrency; this module enforces the ceiling.
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


def _to_int_version(headers: Any) -> Optional[int]:
    """Read ``Last-Modified-Version`` from response headers as an int (case-insensitive)."""
    if headers is None:
        return None
    try:
        v = headers.get("Last-Modified-Version")
        if v is None:
            v = headers.get("last-modified-version")
    except AttributeError:
        return None
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class WriteResult:
    """Structured outcome of a gateway write.

    For a multi-object create, the per-index maps mirror the Web API envelope. For a single-object
    update/delete (HTTP 204), the maps are empty but ``item_keys`` and ``last_modified_version`` are set.
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
    """Parse a multi-object Web API write response (HTTP 200) into a :class:`WriteResult`.

    Zotero returns ``successful`` (index->object), ``success`` (index->key), ``unchanged``
    (index->key), and ``failed`` (index->{key,code,message}). ``item_keys`` is taken from ``success``
    (created/updated keys), falling back to the keys inside ``successful`` objects.
    """
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
    """Structured, versioned writes over the Zotero Web API v3.

    Args:
        transport: a duck-typed transport with
            ``request(method, path, *, json=None, headers=None, params=None) -> resp`` where ``resp``
            exposes ``.status_code: int``, ``.json() -> Any`` and ``.headers`` (a mapping). The real
            transport injects the base URL + auth headers; tests inject a fake.
        batch_limit: the per-request object ceiling (pinned 50).
    """

    def __init__(self, transport: Any, *, batch_limit: int = BATCH_LIMIT):
        self._t = transport
        self.batch_limit = batch_limit

    # ── create (POST array) → envelope ──────────────────────────────────────────
    def create_items(self, library_id: int, objects: list[dict]) -> WriteResult:
        """POST a batch of new items; return the parsed structured envelope."""
        if len(objects) > self.batch_limit:
            raise GatewayError(
                f"create batch of {len(objects)} exceeds the {self.batch_limit}-object limit "
                "(chunk upstream in P0-batch-concurrency)")
        resp = self._t.request(
            "POST", f"/users/{library_id}/items",
            json=objects, headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            raise GatewayError(f"create_items: unexpected HTTP {resp.status_code}")
        return parse_write_envelope(resp.json(), resp.headers)

    # ── update (single PATCH, versioned, 412 re-GET + retry once) ───────────────
    def update_item(self, library_id: int, item_key: str, data: dict,
                    version: Optional[int]) -> WriteResult:
        """PATCH a single item. Requires ``version``; retries once on 412 with the fresh version.

        Default to PATCH (omitted scalars untouched). Array properties (collections/tags/creators/
        relations) are complete-list replaces — callers must pre-compute the union (the merge engine
        does this); the gateway does not second-guess the supplied ``data``.
        """
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
        return self._t.request(
            "PATCH", f"/users/{library_id}/items/{item_key}", json=data,
            headers={"Content-Type": "application/json",
                     "If-Unmodified-Since-Version": str(version)})

    def _current_item_version(self, library_id: int, item_key: str) -> int:
        resp = self._t.request("GET", f"/users/{library_id}/items/{item_key}")
        body = resp.json() or {}
        v = body.get("version")
        if v is None:
            v = (body.get("data") or {}).get("version")
        if v is None:
            raise GatewayError(f"could not read current version for {item_key}")
        return int(v)

    # ── delete (versioned; 412 → caller re-resolves) ────────────────────────────
    def delete_items(self, library_id: int, item_keys: list[str],
                     version: Optional[int]) -> WriteResult:
        """DELETE (trash) up to 50 items, gated by the library version.

        On 412 this raises :class:`ConcurrencyConflictError` rather than blindly re-deleting — a
        concurrent edit may have touched a target item, so the caller (``commit_merge``) re-resolves
        or routes to rollback. Trashes, never purges.
        """
        if version is None:
            raise VersionMissingError(
                "delete_items requires the library version; version-less deletes are rejected.")
        if len(item_keys) > self.batch_limit:
            raise GatewayError(
                f"delete batch of {len(item_keys)} exceeds the {self.batch_limit}-object limit")
        resp = self._t.request(
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
