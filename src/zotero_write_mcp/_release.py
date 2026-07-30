"""Release identity for Routine Supervised v1.0 (PRD DEP-002).

The serving engine must be able to report WHICH reviewed code it is running, machine-checkably.
Because a commit cannot embed its own hash, identity is anchored two ways:

* ``RELEASE_LABEL`` — the human release name, bumped as part of each release commit;
* ``source_digest()`` — a deterministic sha256 over the installed package's own Python sources
  (sorted relative path + content). The reviewer computes the same digest over the reviewed
  release tree (``python -m zotero_write_mcp._release`` from the repo root, or by calling
  ``source_digest()`` in any checkout) and compares it against the running server's
  ``engine_identity`` response. Equal digests == byte-identical engine sources; an editable
  install serving a drifted tree, or a stale frozen build, reports a different digest.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

RELEASE_LABEL = "routine-supervised-v1.0-rc1"


def source_digest(package_dir: "Path | str | None" = None) -> str:
    """sha256 over ``sorted((relpath, sha256(bytes)))`` of every ``*.py`` file in the package
    directory. Deterministic across platforms (paths normalized to forward slashes, bytes hashed
    raw). ``.pyc``/caches are excluded by construction."""
    root = Path(package_dir) if package_dir else Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(root.glob("*.py"), key=lambda p: p.name):
        h.update(path.name.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def identity() -> dict:
    from zotero_write_mcp import __version__
    pkg = Path(__file__).resolve().parent
    return {
        "package_version": __version__,
        "release_label": RELEASE_LABEL,
        "source_digest": source_digest(pkg),
        "module_path": str(pkg),
        "is_editable_src_tree": (pkg.parent.name == "src"),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(identity(), indent=2))
