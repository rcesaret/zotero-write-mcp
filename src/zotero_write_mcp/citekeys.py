"""Citekey-collision + tex.ids alias-survival scanner (S5a F7) — read-only, whole-library.

Citekeys are load-bearing: the downstream Pandoc ``@citekey`` manuscript pipeline resolves through
them (citation-discipline.md). The per-cluster verify check #11 protects ONE merge; this module scans
the WHOLE library. Pure over already-fetched item dicts — makes no network calls itself, so it is
unit-testable offline; the CLI script / MCP tool wrapper supplies the items (via ``webscan.web_items``)
and an injectable trashed-item lookup.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from zotero_write_mcp.merge import _citekey_from_extra, _tex_ids_of


def _data(item: dict) -> dict:
    return item.get("data", item)


def item_citekey(item: dict) -> Optional[str]:
    """Mirrors ``WebClusterReader.get_citekey``'s precedence: the pinned ``extra`` ``Citation Key:``
    line, else the ``citationKey`` field."""
    d = _data(item)
    return _citekey_from_extra(d.get("extra")) or d.get("citationKey") or None


def scan_citekey_collisions(items: list) -> dict:
    """Duplicate BBT citekeys across the live (non-trashed) library — a collision silently breaks the
    Pandoc pipeline. Groups by citekey; anything with more than one item is a collision."""
    by_key: dict = {}
    keyless = 0
    for it in items:
        d = _data(it)
        key = d.get("key") or it.get("key")
        ck = item_citekey(it)
        if not ck:
            keyless += 1
            continue
        by_key.setdefault(ck, []).append(key)
    collisions = {ck: keys for ck, keys in by_key.items() if len(keys) > 1}
    return {
        "items_scanned": len(items),
        "with_citekey": len(items) - keyless,
        "keyless": keyless,
        "unique_citekeys": len(by_key),
        "collisions": collisions,
        "collision_count": len(collisions),
    }


def scan_tex_ids_aliases(items: list, trashed_lookup: Callable[[str], Optional[dict]]) -> dict:
    """For every LIVE item carrying a ``dc:replaces`` relation (a merge survivor), confirm each
    target's own pinned citekey survives as a ``tex.ids:`` alias on the survivor's ``extra`` — the
    owner's explicit feature so a manuscript citing a trashed dup's ``@citekey`` still resolves.

    ``trashed_lookup(target_key) -> item-dict-or-None`` fetches ONE trashed item by exact key (the
    library-wide pager excludes trash by default); injectable so this is unit-testable offline and so
    the caller controls read pacing over the merge-survivor set (currently ~151 pairs from S2).
    """
    checked, missing_alias, no_citekey_on_target, unreadable = [], [], [], []
    for it in items:
        d = _data(it)
        master_key = d.get("key") or it.get("key")
        dc = (d.get("relations") or {}).get("dc:replaces")
        if not dc:
            continue
        targets = dc if isinstance(dc, list) else [dc]
        alias_list = _tex_ids_of(d.get("extra"))
        for uri in targets:
            target_key = str(uri).rstrip("/").rsplit("/", 1)[-1]
            checked.append({"master": master_key, "target": target_key})
            try:
                target_item = trashed_lookup(target_key)
            except Exception as e:
                unreadable.append({"master": master_key, "target": target_key,
                                   "error": f"{type(e).__name__}: {e}"})
                continue
            if target_item is None:
                unreadable.append({"master": master_key, "target": target_key, "error": "not found"})
                continue
            target_ck = item_citekey(target_item)
            if not target_ck:
                no_citekey_on_target.append({"master": master_key, "target": target_key})
                continue
            if target_ck not in alias_list:
                missing_alias.append({"master": master_key, "target": target_key,
                                      "expected_alias": target_ck, "actual_tex_ids": alias_list})
    return {
        "dc_replaces_pairs_checked": len(checked),
        "missing_alias": missing_alias,
        "missing_alias_count": len(missing_alias),
        "target_had_no_citekey": no_citekey_on_target,
        "unreadable_targets": unreadable,
    }
