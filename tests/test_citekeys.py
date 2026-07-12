"""Unit tests for S5a F7 — citekey-collision + tex.ids alias-survival scanner (citekeys.py)."""
from zotero_write_mcp.citekeys import item_citekey, scan_citekey_collisions, scan_tex_ids_aliases


def _item(key, *, citation_key=None, extra=None, relations=None):
    data = {"key": key}
    if citation_key is not None:
        data["citationKey"] = citation_key
    if extra is not None:
        data["extra"] = extra
    if relations is not None:
        data["relations"] = relations
    return {"key": key, "data": data}


def test_item_citekey_prefers_pinned_extra_over_citation_key_field():
    it = _item("A1", citation_key="fallbackKey2020", extra="Citation Key: pinnedKey2020")
    assert item_citekey(it) == "pinnedKey2020"


def test_item_citekey_falls_back_to_citation_key_field():
    it = _item("A1", citation_key="onlyKey2020")
    assert item_citekey(it) is None or item_citekey(it) == "onlyKey2020"
    assert item_citekey(it) == "onlyKey2020"


def test_item_citekey_none_when_keyless():
    assert item_citekey(_item("A1")) is None


def test_scan_citekey_collisions_finds_a_real_collision():
    items = [
        _item("A1", citation_key="sandersBasin1979"),
        _item("A2", citation_key="sandersBasin1979"),   # collision
        _item("A3", citation_key="parsonsValley1971"),
        _item("A4"),                                     # keyless
    ]
    rep = scan_citekey_collisions(items)
    assert rep["items_scanned"] == 4
    assert rep["keyless"] == 1
    assert rep["with_citekey"] == 3
    assert rep["unique_citekeys"] == 2
    assert rep["collision_count"] == 1
    assert set(rep["collisions"]["sandersBasin1979"]) == {"A1", "A2"}


def test_scan_citekey_collisions_clean_library_reports_zero():
    items = [_item("A1", citation_key="a2020"), _item("A2", citation_key="b2020")]
    rep = scan_citekey_collisions(items)
    assert rep["collision_count"] == 0
    assert rep["collisions"] == {}


def test_scan_tex_ids_aliases_confirms_survival():
    survivor = _item(
        "MASTER", extra="tex.ids: dupKey2020",
        relations={"dc:replaces": ["http://zotero.org/users/1/items/DUP1"]},
    )
    trashed = {"DUP1": _item("DUP1", citation_key="dupKey2020")}
    rep = scan_tex_ids_aliases([survivor], lambda k: trashed.get(k))
    assert rep["dc_replaces_pairs_checked"] == 1
    assert rep["missing_alias"] == []
    assert rep["missing_alias_count"] == 0


def test_scan_tex_ids_aliases_catches_a_dropped_alias():
    survivor = _item(
        "MASTER", extra=None,        # alias never landed
        relations={"dc:replaces": ["http://zotero.org/users/1/items/DUP1"]},
    )
    trashed = {"DUP1": _item("DUP1", citation_key="dupKey2020")}
    rep = scan_tex_ids_aliases([survivor], lambda k: trashed.get(k))
    assert rep["missing_alias_count"] == 1
    assert rep["missing_alias"][0]["expected_alias"] == "dupKey2020"
    assert rep["missing_alias"][0]["master"] == "MASTER"


def test_scan_tex_ids_aliases_skips_items_without_dc_replaces():
    plain = _item("A1", extra="tex.ids: x")
    rep = scan_tex_ids_aliases([plain], lambda k: None)
    assert rep["dc_replaces_pairs_checked"] == 0


def test_scan_tex_ids_aliases_reports_unreadable_target():
    survivor = _item("MASTER", relations={"dc:replaces": ["http://zotero.org/users/1/items/GONE"]})
    rep = scan_tex_ids_aliases([survivor], lambda k: None)   # lookup returns None -> not found
    assert len(rep["unreadable_targets"]) == 1
    assert rep["unreadable_targets"][0]["target"] == "GONE"


def test_scan_tex_ids_aliases_reports_lookup_exception_without_raising():
    survivor = _item("MASTER", relations={"dc:replaces": ["http://zotero.org/users/1/items/BAD"]})

    def boom(key):
        raise RuntimeError("network down")

    rep = scan_tex_ids_aliases([survivor], boom)
    assert len(rep["unreadable_targets"]) == 1
    assert "network down" in rep["unreadable_targets"][0]["error"]


def test_scan_tex_ids_aliases_target_has_no_citekey():
    survivor = _item("MASTER", relations={"dc:replaces": ["http://zotero.org/users/1/items/DUP1"]})
    trashed = {"DUP1": _item("DUP1")}   # keyless secondary — nothing to alias
    rep = scan_tex_ids_aliases([survivor], lambda k: trashed.get(k))
    assert rep["target_had_no_citekey"] == [{"master": "MASTER", "target": "DUP1"}]
    assert rep["missing_alias_count"] == 0
