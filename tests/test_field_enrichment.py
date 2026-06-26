"""Phase-B field-level metadata reconciliation: enrichment applied to the survivor, verify-gated.

The merge survivor's scalar fields are set to the owner-approved best-of values, each taken VERBATIM from
the named source member (no fabrication possible). verify_merge check #3, given the same field_sources,
confirms the live survivor equals EXACTLY that approved record — catching a missing, partial, or wrong
enrichment. With no field_sources the gate is pure survivor preservation (backward compatible)."""
import copy

from zotero_write_mcp.merge import (
    snapshot_cluster, compute_merge_projection, verify_merge, _enriched_fields,
)
from zotero_write_mcp.provenance import ProvenanceStore


class _Reader:
    def __init__(self, items):
        self._items = items

    def get_item(self, key):
        return self._items[key]

    def get_children(self, key):
        return []

    def get_annotations(self, key):
        return []

    def get_citekey(self, key):
        return None


def _item(key, version, **data):
    d = {"key": key, "version": version, "itemType": "journalArticle", **data}
    return {"key": key, "version": version, "data": d}


def _snap(tmp_path):
    # survivor M1 has a SHORT title + no publisher; secondary M2 has the FULL title + a publisher.
    items = {
        "M1": _item("M1", 100, title="Basin of Mexico", date="1979", collections=[], tags=[], relations={}),
        "M2": _item("M2", 101, title="Basin of Mexico: An Ecological Study", date="1979",
                    publisher="Academic Press", collections=[], tags=[], relations={}),
    }
    return snapshot_cluster(_Reader(items), "M1", ["M2"], prov=ProvenanceStore(tmp_path))


FS = {"title": "M2", "publisher": "M2"}   # approved: take title + publisher from M2


def _failed(report):
    return {c.name for c in report.failed}


def test_enriched_fields_resolves_verbatim_from_source(tmp_path):
    enr = _enriched_fields(_snap(tmp_path), FS)
    assert enr["title"] == "Basin of Mexico: An Ecological Study"   # M2's value, verbatim
    assert enr["publisher"] == "Academic Press"


def test_enriched_fields_skips_unknown_source_or_missing_field(tmp_path):
    enr = _enriched_fields(_snap(tmp_path), {"title": "NOPE", "DOI": "M1"})  # bad source; M1 lacks DOI
    assert enr == {}


def test_projection_applies_enrichment(tmp_path):
    proj = compute_merge_projection(_snap(tmp_path), field_sources=FS)
    assert proj.items["M1"].fields["title"] == "Basin of Mexico: An Ecological Study"
    assert proj.items["M1"].fields["publisher"] == "Academic Press"


def test_verify_accepts_correctly_enriched_master(tmp_path):
    snap = _snap(tmp_path)
    proj = compute_merge_projection(snap, field_sources=FS)
    assert verify_merge(snap, proj, field_sources=FS).passed


def test_verify_rejects_unapplied_enrichment(tmp_path):
    """The live survivor's title was NOT enriched to the approved value -> check #3 fails."""
    snap = _snap(tmp_path)
    proj = copy.deepcopy(compute_merge_projection(snap, field_sources=FS))
    proj.items["M1"].fields["title"] = "Basin of Mexico"          # left as the survivor's OLD title
    r = verify_merge(snap, proj, field_sources=FS)
    assert not r.passed and "master-scalar-preservation" in _failed(r)


def test_verify_rejects_wrong_value_enrichment(tmp_path):
    """A survivor field set to anything OTHER than the approved source value is caught (no fabrication)."""
    snap = _snap(tmp_path)
    proj = copy.deepcopy(compute_merge_projection(snap, field_sources=FS))
    proj.items["M1"].fields["title"] = "Something Fabricated"
    r = verify_merge(snap, proj, field_sources=FS)
    assert not r.passed and "master-scalar-preservation" in _failed(r)


def test_no_field_sources_is_pure_preservation(tmp_path):
    """Backward compat: with no field_sources, changing the survivor's title still fails (preservation)."""
    snap = _snap(tmp_path)
    proj = copy.deepcopy(compute_merge_projection(snap))
    assert verify_merge(snap, proj).passed                        # untouched projection passes
    proj.items["M1"].fields["title"] = "Basin of Mexico: An Ecological Study"   # changed WITHOUT approval
    r = verify_merge(snap, proj)
    assert not r.passed and "master-scalar-preservation" in _failed(r)
