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
                # Newline-defensive append (Routine Supervised v1.0, LOG-002; intent of engine commit
                # ef9bf67 reimplemented on the clean base): if the file does not currently end in a
                # newline — a crash between write and fsync, or a torn line from a cross-process writer
                # (the lock is threading-only, and operator scripts open their own store on the same
                # ZOTERO_PROV_DIR) — appending directly would CONCATENATE this record onto the broken
                # tail, making BOTH unparseable. Terminating the tail first costs one seek and loses
                # nothing: the torn fragment stays on its own line where the integrity scan reports it.
                if self._needs_leading_newline():
                    f.write("\n")
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return rec

    def _needs_leading_newline(self) -> bool:
        """True iff the log is non-empty and its last byte is not a newline. Never raises — on any read
        error it returns False, which preserves plain-append behaviour rather than blocking a write."""
        try:
            if not self.prov_path.exists() or self.prov_path.stat().st_size == 0:
                return False
            with open(self.prov_path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) != b"\n"
        except OSError:
            return False

    def iter_records(self) -> Iterator[dict]:
        """Yield every PROV record in append order.

        A malformed line (crash mid-append, or a torn line from a cross-process writer) is SKIPPED,
        not fatal, and does not stop iteration (LOG-002; intent of engine commit ef9bf67). The old
        ``break`` behaviour made every record AFTER a malformed line invisible, so a single stray byte
        blinded readers to the whole tail — including a real verify failure. Skipping keeps the tail
        visible; the malformed line itself is NOT silently forgotten: :meth:`scan_integrity` reports
        every bad line, and destructive readiness fails closed until each one is explicitly accepted
        (LOG-001). Query paths get the valid records; gating paths must consult ``scan_integrity``.
        """
        if not self.prov_path.exists():
            return
        with open(self.prov_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if len(line) > self.MAX_LINE_BYTES:
                    continue   # oversized line: unparseable by policy; scan_integrity reports it
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue   # skip, never `break` — a torn line must not hide the records after it
                if isinstance(rec, dict):   # F-3: non-dict JSON is damage (scan says so) — never yield it,
                    yield rec               # or every .get() consumer crashes after owner acceptance

    # ── Log integrity (Routine Supervised v1.0: LOG-001 fail-closed readiness) ──

    # A line longer than this is treated as malformed without parsing (bounded scan: no pathological
    # single-line allocation can stall the integrity gate).
    MAX_LINE_BYTES = 10_000_000
    # The scan reports at most this many bad lines in detail (bounded output; the count is still exact).
    MAX_BAD_LINE_DETAIL = 50

    ACCEPT_ACTIVITY = "log_damage_accepted"

    def scan_integrity(self) -> dict:
        """Structurally validate the ENTIRE current log and report an explicit integrity state.

        Returns ``{"status", "total_lines", "valid_records", "bad_lines", "unaccepted", "accepted"}``
        where ``status`` is:

        * ``"ok"``      — every non-empty line parses as a JSON object;
        * ``"accepted_damage"`` — malformed line(s) exist but EVERY one is covered by an explicit
          ``log_damage_accepted`` record (owner-recorded resolution; see
          ``scripts/accept_prov_damage.py``). Destructive work may proceed; the damage stays visible.
        * ``"blocked"`` — at least one malformed line has no acceptance record. Destructive operations
          must refuse (LOG-001: malformed state blocks rather than silently omitting evidence).

        Each bad line is identified by ``(line_no, sha256(raw bytes))`` — stable in an append-only file.
        ``at_eof`` distinguishes a torn tail (crash mid-append; the common benign shape) from mid-file
        damage. This is deliberately NOT a cryptographic claim (LOG-003): in the supervised single-
        principal deployment the acceptance record is same-principal-writable; it provides visibility
        and an audit trail, not authentication.

        F-2 (review, MAJOR): ``status``/``unaccepted_count`` are computed from EVERY damaged line —
        only the DETAIL lists (``bad_lines``/``unaccepted``/``accepted``) are capped at
        ``MAX_BAD_LINE_DETAIL`` entries each, with ``unaccepted`` prioritising still-blocking lines
        so successive accept-and-rescan rounds always surface the remaining damage.
        """
        bad_all: list = []            # EVERY damaged line (identity + detail) — uncapped
        accepted_marks: set = set()
        total = 0
        valid = 0
        if not self.prov_path.exists():
            return {"status": "ok", "total_lines": 0, "valid_records": 0,
                    "bad_line_count": 0, "unaccepted_count": 0,
                    "bad_lines": [], "unaccepted": [], "accepted": []}
        with open(self.prov_path, "rb") as f:
            raw_lines = f.read().split(b"\n")
        # split() yields a final "" element when the file ends with a newline; drop it.
        if raw_lines and raw_lines[-1] == b"":
            raw_lines.pop()
        n_lines = len(raw_lines)
        for idx, raw in enumerate(raw_lines, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            total += 1
            rec = None
            if len(stripped) <= self.MAX_LINE_BYTES:
                try:
                    rec = json.loads(stripped.decode("utf-8", errors="strict"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    rec = None
            if isinstance(rec, dict):
                valid += 1
                if rec.get("activity") == self.ACCEPT_ACTIVITY:
                    p = rec.get("params") or {}
                    accepted_marks.add((p.get("line_no"), p.get("line_sha256")))
                continue
            bad_all.append({
                "line_no": idx,
                "line_sha256": sha256_hex(stripped),
                "preview": stripped[:120].decode("utf-8", errors="replace"),
                "at_eof": idx == n_lines,
                "reason": ("oversized" if len(stripped) > self.MAX_LINE_BYTES else "malformed"),
            })
        unaccepted_all = [b for b in bad_all if (b["line_no"], b["line_sha256"]) not in accepted_marks]
        accepted_all = [b for b in bad_all if (b["line_no"], b["line_sha256"]) in accepted_marks]
        # Status from the FULL sets; details capped.
        status = "ok" if not bad_all else ("accepted_damage" if not unaccepted_all else "blocked")
        cap = self.MAX_BAD_LINE_DETAIL
        return {"status": status, "total_lines": total, "valid_records": valid,
                "bad_line_count": len(bad_all), "unaccepted_count": len(unaccepted_all),
                "bad_lines": bad_all[:cap], "unaccepted": unaccepted_all[:cap],
                "accepted": accepted_all[:cap]}

    def accept_log_damage(self, line_no: int, line_sha256: str, *, note: str,
                          operator: str = "owner") -> dict:
        """Record the owner's explicit resolution of ONE malformed log line, identified exactly by
        ``(line_no, sha256)``. Intended to be invoked out-of-band via ``scripts/accept_prov_damage.py``
        after human inspection — it is intentionally NOT exposed as an MCP tool, so the agent workflow
        cannot casually self-acknowledge damage. The damaged line is never rewritten or removed
        (append-only store); it simply stops blocking readiness while remaining permanently visible in
        every future ``scan_integrity`` report."""
        if not note or not note.strip():
            raise ValueError("accept_log_damage requires a non-empty human note")
        return self.record(
            activity=self.ACCEPT_ACTIVITY,
            agent=operator,
            params={"line_no": int(line_no), "line_sha256": line_sha256, "note": note},
        )

    def query(self, item_key: str) -> list[dict]:
        """All PROV records for a given item, in append order (the basis for ``query_provenance``)."""
        return [r for r in self.iter_records() if r.get("entity", {}).get("item_key") == item_key]

    def all_records(self) -> list[dict]:
        return list(self.iter_records())

    def count(self) -> int:
        return sum(1 for _ in self.iter_records())
