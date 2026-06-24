"""Provenance + content-addressed blob store for the zotero-write transaction engine.

A W3C-PROV-flavored, append-only audit log (JSONL) plus a content-addressed blob store. This
subsystem is stood up BEFORE the first automated write (Phase 0): every mutation records a PROV
entry and, for reversible operations, persists before/after images to the blob store. The
``snapshot_id`` recorded as ``was_derived_from`` is exactly what ``rollback_merge`` will consume —
"the audit trail IS the rollback index" (DR5; ADR-008/009).

PROV mapping
------------
* **Entity**   -> the affected item: ``item_key`` + before/after ``json_sha256`` (+ optional blobs).
* **Activity** -> the mutating tool/operation: ``activity`` + ``params``.
* **Agent**    -> who authorized it: ``agent`` (skill/sub-agent) + ``tool_version``.
* plus ``source``, ``confidence``, ``was_derived_from`` (= ``snapshot_id``), and ``reverse``.

Design guarantees
-----------------
* **Append-only.** ``prov.jsonl`` is only ever appended to (one JSON object per line); never rewritten.
* **Content-addressed blobs.** Stored at ``blobs/<sha256[:2]>/<sha256>``; writes are atomic
  (temp + ``os.replace``) and idempotent (identical content -> same path, no rewrite).
* **Canonical hashing.** ``json_sha256`` uses sorted keys + compact separators, so two equal objects
  always hash equal regardless of key order.
* **Durable.** Each append is flushed and ``fsync``-ed; a crash mid-append cannot lose an earlier
  record, and a partially-written trailing line is detected and skipped on read.

Stdlib-only (no third-party deps), so it can be imported and tested without the MCP runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


# ── Hashing helpers ───────────────────────────────────────────────────────────

def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON bytes: sorted keys, compact separators, UTF-8.

    Two objects that are equal as data serialize to identical bytes regardless of key order,
    so their hashes match. This is the basis for ``json_sha256`` and JSON blob addressing.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_sha256(obj: Any) -> Optional[str]:
    """sha256 hex of an object's canonical JSON. Returns ``None`` when ``obj`` is ``None``."""
    if obj is None:
        return None
    return sha256_hex(canonical_json(obj))


# ── Store ─────────────────────────────────────────────────────────────────────

class ProvenanceStore:
    """Append-only PROV log + content-addressed blob store rooted at a directory.

    Thread-safe for appends within a single process: a lock serializes writes to ``prov.jsonl``.
    (The gateway is single-process, so this is sufficient; cross-process use would need file locks.)
    """

    PROV_FILE = "prov.jsonl"
    BLOB_DIR = "blobs"

    def __init__(self, root: "os.PathLike[str] | str"):
        self.root = Path(root)
        self.prov_path = self.root / self.PROV_FILE
        self.blob_root = self.root / self.BLOB_DIR
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.prov_path.touch(exist_ok=True)
        self._lock = threading.Lock()

    # ── Blob store ────────────────────────────────────────────────────────────

    def _blob_path(self, digest: str) -> Path:
        return self.blob_root / digest[:2] / digest

    def put_blob(self, data: bytes) -> str:
        """Store bytes content-addressed; return the sha256 hex. Idempotent and atomic."""
        digest = sha256_hex(data)
        dest = self._blob_path(digest)
        if dest.exists():
            return digest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f"{digest}.tmp-{uuid.uuid4().hex}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)  # atomic on the same filesystem
        return digest

    def put_json_blob(self, obj: Any) -> Optional[str]:
        """Store an object's canonical JSON as a blob; return its sha256 (``None`` if ``obj`` is ``None``)."""
        if obj is None:
            return None
        return self.put_blob(canonical_json(obj))

    def has_blob(self, digest: str) -> bool:
        return self._blob_path(digest).exists()

    def get_blob(self, digest: str) -> bytes:
        return self._blob_path(digest).read_bytes()

    def get_json_blob(self, digest: str) -> Any:
        return json.loads(self.get_blob(digest).decode("utf-8"))

    # ── PROV log ────────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        activity: str,
        item_key: Optional[str] = None,
        before: Any = None,
        after: Any = None,
        store_blobs: bool = True,
        agent: Optional[str] = None,
        tool_version: Optional[str] = None,
        params: Optional[dict] = None,
        source: Optional[str] = None,
        confidence: Optional[float] = None,
        snapshot_id: Optional[str] = None,
        reverse: Optional[dict] = None,
        ts: Optional[str] = None,
    ) -> dict:
        """Append a PROV record and return it.

        Args:
            activity: the mutating tool/operation name (PROV Activity). Required.
            item_key: the affected item key (PROV Entity).
            before/after: the item JSON images. Their ``json_sha256`` is always recorded; when
                ``store_blobs`` is true the full images are persisted to the blob store so the
                mutation is reversible.
            agent/tool_version: who/what authorized and performed it (PROV Agent).
            params: the tool parameters.
            source/confidence: provenance of derived values (e.g., "OpenAlex DOI", match score).
            snapshot_id: links the record to the rollback index (``was_derived_from``).
            reverse: the inverse operation needed to undo this mutation.
            ts: ISO timestamp; defaults to ``datetime.now(timezone.utc)``.

        The record is flushed + ``fsync``-ed before returning, so it is durable on success.
        """
        if not activity:
            raise ValueError("PROV record requires a non-empty 'activity'")
        rec = {
            "prov_id": uuid.uuid4().hex,
            "ts": ts or datetime.now(timezone.utc).isoformat(),
            "activity": activity,
            "agent": agent,
            "tool_version": tool_version,
            "params": params,
            "entity": {
                "item_key": item_key,
                "before_sha256": json_sha256(before),
                "after_sha256": json_sha256(after),
                "before_blob": self.put_json_blob(before) if store_blobs else None,
                "after_blob": self.put_json_blob(after) if store_blobs else None,
            },
            "source": source,
            "confidence": confidence,
            "was_derived_from": snapshot_id,
            "reverse": reverse,
        }
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            with open(self.prov_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return rec

    def iter_records(self) -> Iterator[dict]:
        """Yield every PROV record in append order.

        A partially-written trailing line (e.g., a crash mid-append) is detected and stops
        iteration cleanly rather than raising — earlier records remain readable.
        """
        if not self.prov_path.exists():
            return
        with open(self.prov_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    break

    def query(self, item_key: str) -> list[dict]:
        """All PROV records for a given item, in append order (the basis for ``query_provenance``)."""
        return [r for r in self.iter_records() if r.get("entity", {}).get("item_key") == item_key]

    def all_records(self) -> list[dict]:
        return list(self.iter_records())

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())
