"""S5a F5 — backup-before-live: snapshot + PROV-blob confirmation, then a dated read-only export
BACKSTOP. Read-only against the library (GET only, via ``snapshot_cluster``'s injected reader); the
only writes are new PROV snapshot records (the sanctioned append-only audit — already how every merge
starts) and a local dated export file. NEVER a Zotero mutation.

The PRIMARY restore path stays ``rollback_merge`` + trash recovery (merge-safety.md) — this export is
a belt-and-suspenders backstop, not the rollback mechanism (REV5 finding 5: rollback durability
depends on trash not being emptied).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from zotero_write_mcp.merge import snapshot_cluster
from zotero_write_mcp.provenance import ProvenanceStore


@dataclass
class BackupResult:
    pairs_backed_up: int
    snapshot_ids: list = field(default_factory=list)
    blob_confirmed: list = field(default_factory=list)   # snapshot_ids whose before-image blob is CONFIRMED present
    blob_missing: list = field(default_factory=list)      # snapshot_ids whose blob is UNEXPECTEDLY absent (alert)
    export_path: Optional[str] = None
    export_item_count: int = 0


def backup_before_live(pairs: list, reader, prov: ProvenanceStore, *,
                       export_fn: Optional[Callable[[list], object]] = None,
                       export_dir: Optional[str] = None, now: Optional[datetime] = None) -> BackupResult:
    """``pairs``: ``[{"master": key, "dups": [key, ...]}, ...]``.

    For each pair: (a) ``snapshot_cluster`` (persists the full before-image to PROV — the belt);
    confirm its before-image blob is actually present in the blob store (a missing blob after a
    successful snapshot call would mean the rollback index is broken — surfaced via ``blob_missing``,
    never silently trusted). (b) if ``export_fn`` is given, it is called once with the deduped list of
    every affected key (master + dups) and its return value is written to a **dated** JSON file (never
    overwrites a prior backup, per file-handling.md) — the suspenders.
    """
    now = now or datetime.now(timezone.utc)
    snapshot_ids, blob_confirmed, blob_missing = [], [], []
    all_keys: list = []
    for pair in pairs:
        master = pair["master"]
        dups = list(pair.get("dups") or [])
        snap = snapshot_cluster(reader, master, dups, prov=prov)
        snapshot_ids.append(snap.snapshot_id)
        all_keys.append(master)
        all_keys.extend(dups)
        rec = next((r for r in prov.all_records()
                   if r.get("activity") == "snapshot_cluster"
                   and r.get("was_derived_from") == snap.snapshot_id),
                  None)
        blob = (rec.get("entity") or {}).get("before_blob") if rec else None
        if blob and prov.has_blob(blob):
            blob_confirmed.append(snap.snapshot_id)
        else:
            blob_missing.append(snap.snapshot_id)

    export_path = None
    export_item_count = 0
    if export_fn is not None:
        export = export_fn(list(dict.fromkeys(all_keys)))
        export_item_count = len(export) if isinstance(export, list) else len(export.get("items", []))
        out_dir = Path(export_dir or "exit-gate-runs/backups")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        export_path = str(out_dir / f"backup-{stamp}.json")
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump({"ts": now.isoformat(), "snapshot_ids": snapshot_ids, "export": export}, f,
                      indent=1, ensure_ascii=False)

    return BackupResult(
        pairs_backed_up=len(pairs), snapshot_ids=snapshot_ids,
        blob_confirmed=blob_confirmed, blob_missing=blob_missing,
        export_path=export_path, export_item_count=export_item_count,
    )
