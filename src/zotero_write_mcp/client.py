"""Zotero hybrid API client: local reads, web API writes.

All mutations route through the write gateway v2 (the single mutation chokepoint) and every mutation
is recorded to the provenance store before the method returns (Phase 0: P0-legacy-retire). Reads stay
on the local API.
"""
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from zotero_write_mcp import __version__
from zotero_write_mcp.gateway import HttpxTransport, WriteGateway
from zotero_write_mcp.provenance import ProvenanceStore


class ZoteroClient:
    """Hybrid client: local API for reads, web API (api.zotero.org) for writes.

    The Zotero local API (port 23119) is read-only. All write operations
    (POST/PUT/PATCH/DELETE) are routed through the Zotero Web API v3, which
    requires an API key with write permissions.
    """

    def __init__(self, *, gateway: Optional[WriteGateway] = None,
                 prov: Optional[ProvenanceStore] = None):
        # ── Local API (reads) ──────────────────────────────────────
        self.local_url = os.environ.get(
            "ZOTERO_LOCAL_URL", "http://127.0.0.1:23119/api"
        ).rstrip("/")
        self._local_headers = {"Zotero-Allowed-Request": "true"}

        # ── Web API (writes) ──────────────────────────────────────
        self.web_url = "https://api.zotero.org"
        self._api_key = os.environ.get("ZOTERO_API_KEY", "")
        if not self._api_key:
            raise RuntimeError(
                "ZOTERO_API_KEY environment variable is required for write operations. "
                "Generate one at https://www.zotero.org/settings/keys"
            )
        self._web_headers = {
            "Zotero-API-Key": self._api_key,
            "Zotero-API-Version": "3",
        }

        self._library_id: Optional[int] = None
        self._client = httpx.Client(timeout=30.0)

        # ── Write gateway v2 + provenance (the SINGLE mutation path) ──
        # All writes route through the gateway and are logged to PROV before returning. Both are
        # lazy and injectable (tests inject a fake transport + a temp PROV store).
        self._gateway = gateway
        self._prov = prov

    # ── Backward compat ───────────────────────────────────────────
    @property
    def base_url(self) -> str:
        """Alias for local_url (backward compat)."""
        return self.local_url

    @property
    def headers(self) -> dict:
        """Alias for local headers (backward compat)."""
        return self._local_headers

    # ── Library detection ─────────────────────────────────────────

    @property
    def library_id(self) -> int:
        if self._library_id is None:
            self._library_id = self._detect_library_id()
        return self._library_id

    @property
    def lib_prefix(self) -> str:
        return f"{self.local_url}/users/{self.library_id}"

    # ── Write gateway v2 + provenance ─────────────────────────────
    @property
    def gateway(self) -> WriteGateway:
        """The single mutation chokepoint (lazy). Built over the web httpx client + auth headers."""
        if self._gateway is None:
            self._gateway = WriteGateway(
                HttpxTransport(self._client, self.web_url, self._web_headers))
        return self._gateway

    @property
    def prov(self) -> ProvenanceStore:
        """The provenance store (lazy). Root from $ZOTERO_PROV_DIR, else ~/.zotero-write-mcp/prov."""
        if self._prov is None:
            root = os.environ.get("ZOTERO_PROV_DIR") or str(
                Path.home() / ".zotero-write-mcp" / "prov")
            self._prov = ProvenanceStore(root)
        return self._prov

    @staticmethod
    def _envelope_compat(result) -> dict:
        """Reconstruct the legacy {success, successful, unchanged, failed} dict the tools consume."""
        return {
            "success": {str(i): k for i, k in enumerate(result.item_keys)},
            "successful": result.successful,
            "unchanged": result.unchanged,
            "failed": result.failed,
            "last_modified_version": result.last_modified_version,
        }

    def _detect_library_id(self) -> int:
        """Auto-detect library ID from local Zotero instance."""
        r = self._get("/users/0/items", params={"limit": 1, "format": "json"})
        if r and len(r) > 0 and "library" in r[0]:
            return r[0]["library"]["id"]
        r = self._get("/users/0/collections", params={"limit": 1, "format": "json"})
        if r and len(r) > 0 and "library" in r[0]:
            return r[0]["library"]["id"]
        raise RuntimeError("Could not detect Zotero library ID from local API.")

    # ── Local API (reads) ─────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET via local API for fast reads."""
        url = f"{self.local_url}{path}"
        resp = self._client.get(url, headers=self._local_headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    # ── Web API (writes) ──────────────────────────────────────────

    def _web_post(self, path: str, data: Any, extra_headers: Optional[dict] = None) -> Any:
        """POST via Zotero Web API."""
        url = f"{self.web_url}{path}"
        hdrs = {**self._web_headers, "Content-Type": "application/json"}
        if extra_headers:
            hdrs.update(extra_headers)
        resp = self._client.post(url, headers=hdrs, content=json.dumps(data))
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "ok"}

    def _web_put(self, path: str, data: Any, version: int) -> Any:
        """PUT via Zotero Web API."""
        url = f"{self.web_url}{path}"
        hdrs = {
            **self._web_headers,
            "Content-Type": "application/json",
            "If-Unmodified-Since-Version": str(version),
        }
        resp = self._client.put(url, headers=hdrs, content=json.dumps(data))
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "ok"}

    def _web_patch(self, path: str, data: Any, version: int) -> Any:
        """PATCH via Zotero Web API."""
        url = f"{self.web_url}{path}"
        hdrs = {
            **self._web_headers,
            "Content-Type": "application/json",
            "If-Unmodified-Since-Version": str(version),
        }
        resp = self._client.patch(url, headers=hdrs, content=json.dumps(data))
        resp.raise_for_status()
        return resp.json() if resp.content else {"status": "ok"}

    def _web_delete(self, path: str, version: int) -> bool:
        """DELETE via Zotero Web API."""
        url = f"{self.web_url}{path}"
        hdrs = {
            **self._web_headers,
            "If-Unmodified-Since-Version": str(version),
        }
        resp = self._client.delete(url, headers=hdrs)
        resp.raise_for_status()
        return True

    # ── Backward-compat aliases (server.py calls these) ───────────

    def _post(self, path: str, data: Any, extra_headers: Optional[dict] = None) -> Any:
        """Route POST to web API."""
        return self._web_post(path, data, extra_headers)

    def _put(self, path: str, data: Any, version: int) -> Any:
        """Route PUT to web API."""
        return self._web_put(path, data, version)

    def _delete(self, path: str, version: int) -> bool:
        """Route DELETE to web API."""
        return self._web_delete(path, version)

    # ── Web API (reads for version-sensitive ops) ─────────────────

    def _web_get(self, path: str, params: Optional[dict] = None) -> Any:
        """GET via Zotero Web API (for version-accurate reads before writes)."""
        url = f"{self.web_url}{path}"
        resp = self._client.get(url, headers=self._web_headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    # ── Item CRUD ─────────────────────────────────────────────────

    def get_item(self, item_key: str) -> dict:
        """Read item via local API."""
        return self._get(f"/users/{self.library_id}/items/{item_key}")

    def get_item_web(self, item_key: str) -> dict:
        """Read item via web API (guaranteed current version)."""
        return self._web_get(f"/users/{self.library_id}/items/{item_key}")

    def search_items(self, query: str, limit: int = 20,
                     qmode: str = "titleCreatorYear") -> list[dict]:
        return self._get(
            f"/users/{self.library_id}/items",
            params={
                "q": query,
                "qmode": qmode,
                "limit": limit,
                "format": "json",
                "itemType": "-attachment",
            },
        )

    def get_item_template(self, item_type: str) -> dict:
        """Get item template. Try local API first, fall back to web API."""
        try:
            return self._get("/items/new", params={"itemType": item_type})
        except httpx.HTTPStatusError:
            return self._web_get("/items/new", params={"itemType": item_type})

    def create_items(self, items: list[dict]) -> dict:
        """Create items through the write gateway (versioned, chunked, structured); log PROV.

        Returns the legacy ``{success, successful, unchanged, failed}`` envelope the create tools consume.
        """
        result = self.gateway.create_items_chunked(self.library_id, items)
        for i, key in enumerate(result.item_keys):
            after = items[i] if i < len(items) else None
            self.prov.record(
                activity="create_item", item_key=key, after=after,
                agent="zotero-write", tool_version=__version__,
                params={"itemType": (after or {}).get("itemType")})
        return self._envelope_compat(result)

    def update_item(self, item_key: str, data: dict, version: int) -> Any:
        """Update item through the gateway (PATCH; rejects version-less; 412 re-GET + retry); log PROV."""
        result = self.gateway.update_item(self.library_id, item_key, data, version)
        self.prov.record(
            activity="update_item", item_key=item_key, after=data,
            agent="zotero-write", tool_version=__version__,
            params={"fields": sorted(data.keys())})
        return result

    def delete_item(self, item_key: str, version: int) -> bool:
        """Delete (trash) an item through the gateway; log PROV. 412 -> ConcurrencyConflictError."""
        self.gateway.delete_items(self.library_id, [item_key], version)
        self.prov.record(
            activity="delete_item", item_key=item_key,
            agent="zotero-write", tool_version=__version__,
            params={"version": version})
        return True

    def get_all_items(self, limit: int = 100, start: int = 0,
                      item_type: str = "-attachment") -> list[dict]:
        return self._get(
            f"/users/{self.library_id}/items",
            params={
                "limit": limit,
                "start": start,
                "format": "json",
                "itemType": item_type,
            },
        )

    # ── Attachment Operations ─────────────────────────────────────

    def get_item_children(self, item_key: str) -> list[dict]:
        """Get child items (attachments, notes) via local API."""
        return self._get(
            f"/users/{self.library_id}/items/{item_key}/children",
            params={"format": "json"},
        )

    def get_item_children_web(self, item_key: str) -> list[dict]:
        """Get child items via web API (current state after recent writes)."""
        return self._web_get(
            f"/users/{self.library_id}/items/{item_key}/children",
            params={"format": "json"},
        )

    def create_linked_file_attachment(
        self, parent_key: str, file_path: str, title: str, content_type: str
    ) -> dict:
        """DISABLED (S0 C.5) — imported-only attachment policy (.claude/rules/file-handling.md).

        Linked-file attachments store only a local path: they work ONLY on the machine holding the file
        and do NOT sync to cloud / groups / web / mobile. Hard-refused at the engine layer so no path —
        raw MCP, stand-alone mode A, or a misconfigured host — can create a non-syncing linked attachment.
        Use ``create_imported_file_attachment`` instead (imported files sync everywhere).
        """
        raise RuntimeError(
            "create_linked_file_attachment is DISABLED — imported-only policy "
            "(.claude/rules/file-handling.md). Linked attachments do not sync (cloud/groups/web/mobile). "
            "Use create_imported_file_attachment instead."
        )

    def create_imported_file_attachment(
        self, parent_key: str, file_path: str, title: str, content_type: str
    ) -> dict:
        """Create an imported-file attachment via web API file upload protocol.

        Web API file upload:
        1. Create attachment item metadata
        2. Request upload authorization from /items/{key}/file
        3. Upload file bytes to the authorized URL
        """
        # Read file
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        file_size = len(file_bytes)
        file_md5 = hashlib.md5(file_bytes).hexdigest()
        filename = os.path.basename(file_path)

        # Step 1: Create attachment item
        attachment_data = {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "imported_file",
            "title": title,
            "contentType": content_type,
            "charset": "",
            "filename": filename,
            "tags": [],
            "relations": {},
        }
        gw_result = self.gateway.create_items(self.library_id, [attachment_data])
        if not gw_result.item_keys:
            return self._envelope_compat(gw_result)
        att_key = gw_result.item_keys[0]
        success = {"0": att_key}
        self.prov.record(
            activity="attach_file_imported", item_key=parent_key,
            agent="zotero-write", tool_version=__version__,
            params={"attachment_key": att_key, "linkMode": "imported_file", "filename": filename})

        # Step 2: Request upload authorization
        auth_url = f"{self.web_url}/users/{self.library_id}/items/{att_key}/file"
        auth_hdrs = {
            **self._web_headers,
            "Content-Type": "application/x-www-form-urlencoded",
            "If-None-Match": "*",
        }
        auth_body = f"md5={file_md5}&filename={filename}&filesize={file_size}"
        auth_resp = self._client.post(
            auth_url, headers=auth_hdrs, content=auth_body
        )

        if auth_resp.status_code == 200:
            auth_data = auth_resp.json()

            if auth_data.get("exists"):
                # File already on server
                return {"success": success, "uploaded": True, "note": "file already existed"}

            # Step 3: Upload file to the provided URL
            upload_url = auth_data["url"]
            upload_hdrs = {
                "Content-Type": auth_data.get("contentType", content_type),
            }
            # Include any prefix/suffix from the authorization
            upload_body = b""
            if auth_data.get("prefix"):
                upload_body += auth_data["prefix"].encode("latin-1")
            upload_body += file_bytes
            if auth_data.get("suffix"):
                upload_body += auth_data["suffix"].encode("latin-1")

            upload_resp = self._client.post(
                upload_url, headers=upload_hdrs, content=upload_body
            )

            if upload_resp.status_code in (200, 201, 204):
                # Step 4: Register upload with Zotero
                reg_hdrs = {
                    **self._web_headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "If-None-Match": "*",
                }
                reg_body = f"upload={auth_data['uploadKey']}"
                reg_resp = self._client.post(
                    auth_url, headers=reg_hdrs, content=reg_body
                )
                if reg_resp.status_code in (200, 201, 204):
                    return {"success": success, "uploaded": True}
                else:
                    return {
                        "success": success,
                        "uploaded": False,
                        "register_status": reg_resp.status_code,
                        "register_body": reg_resp.text,
                    }
            else:
                return {
                    "success": success,
                    "uploaded": False,
                    "upload_status": upload_resp.status_code,
                }
        elif auth_resp.status_code == 412:
            return {"success": success, "uploaded": True, "note": "file already existed (412)"}
        else:
            return {
                "success": success,
                "uploaded": False,
                "auth_status": auth_resp.status_code,
                "auth_body": auth_resp.text,
            }
