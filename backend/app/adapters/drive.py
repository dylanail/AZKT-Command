"""Google Drive adapter (spec §7.1, §12.3). Read-only over the shared `GoogleApi` client.

Capabilities: `list_children`, `get_file_meta`, `download`, `changes_start_page_token`, `changes_list`
(Drive's changes feed: https://developers.google.com/workspace/drive/api/guides/manage-changes) and
`search_folders` for the folder picker. Nothing here ever writes to Drive or changes source sharing.

`FakeDrive` is the in-memory implementation used by tests: a file tree with ids, parents, owners,
checksums (md5) and versions, plus a changes feed so incremental scans, moves and revoked access can be
exercised without the network.
"""
from __future__ import annotations

import hashlib
from typing import Any

from ..core.errors import ProviderError, Unsupported

FILES_URL = "https://www.googleapis.com/drive/v3/files"
CHANGES_URL = "https://www.googleapis.com/drive/v3/changes"
FILE_FIELDS = ("id,name,mimeType,parents,modifiedTime,createdTime,md5Checksum,size,version,trashed,"
               "owners(displayName,emailAddress),webViewLink,shortcutDetails,capabilities(canDownload),driveId")
LIST_FIELDS = f"nextPageToken,files({FILE_FIELDS})"
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."
READ_SCOPES = ("https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/drive.metadata.readonly",
               "https://www.googleapis.com/auth/drive.file")
WRITE_SCOPE_MARKERS = ("/auth/drive", "/auth/drive.appdata")


def error_kind(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        return str((exc.detail or {}).get("kind") or "unknown_result")
    return "unknown_result"


def is_folder(meta: dict) -> bool:
    return meta.get("mimeType") == FOLDER_MIME


def assert_read_only(scopes: list[str] | None) -> None:
    """Invariant: the ledger/importer integrations request read scopes only (spec §6.1, §7.1)."""
    bad = [s for s in (scopes or []) if any(s.rstrip("/").endswith(m) for m in WRITE_SCOPE_MARKERS)
           and s not in READ_SCOPES]
    if bad:
        raise Unsupported(f"write scopes are never requested for Drive/Sheets: {bad}")


class DriveAdapter:
    """Live Drive client. Typed errors come from GoogleApi (auth_expired / permission_denied / rate_limited / transient)."""

    kind = "live"

    def __init__(self, api):
        self.api = api

    async def list_children(self, folder_id: str, page_token: str | None = None, *, page_size: int = 200) -> dict:
        params = {"q": f"'{folder_id}' in parents and trashed = false", "fields": LIST_FIELDS,
                  "pageSize": page_size, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if page_token:
            params["pageToken"] = page_token
        body = await self.api.request("GET", FILES_URL, params=params)
        return {"files": list(body.get("files") or []), "next_page_token": body.get("nextPageToken")}

    async def get_file_meta(self, file_id: str) -> dict:
        return await self.api.request("GET", f"{FILES_URL}/{file_id}",
                                      params={"fields": FILE_FIELDS, "supportsAllDrives": "true"})

    async def download(self, file_id: str) -> bytes:
        """Bytes of a binary file. Google-native documents are not downloadable as originals."""
        import httpx

        from .google_oauth import access_token
        meta = await self.get_file_meta(file_id)
        if str(meta.get("mimeType", "")).startswith(GOOGLE_NATIVE_PREFIX):
            raise Unsupported(f"{meta.get('name')} is a Google-native document; export is not part of importer media")
        token = await access_token(self.api.db, self.api.conn)
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(f"{FILES_URL}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                            headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            raise ProviderError("drive auth expired", kind="auth_expired")
        if r.status_code == 403:
            raise ProviderError("drive permission denied", kind="permission_denied")
        if r.status_code == 404:
            raise ProviderError("drive file not found", kind="invalid_input")
        if r.status_code == 429:
            raise ProviderError("drive rate limited", kind="rate_limited")
        if r.status_code >= 500:
            raise ProviderError(f"drive transient error {r.status_code}", kind="transient")
        if r.status_code >= 400:
            raise ProviderError(f"drive download failed {r.status_code}", kind="invalid_input")
        return r.content

    async def changes_start_page_token(self) -> str:
        body = await self.api.request("GET", f"{CHANGES_URL}/startPageToken", params={"supportsAllDrives": "true"})
        return str(body.get("startPageToken") or "")

    async def changes_list(self, page_token: str, *, page_size: int = 200) -> dict:
        params = {"pageToken": page_token, "pageSize": page_size, "supportsAllDrives": "true",
                  "includeItemsFromAllDrives": "true", "includeRemoved": "true",
                  "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,time,file({FILE_FIELDS}))"}
        body = await self.api.request("GET", CHANGES_URL, params=params)
        return {"changes": list(body.get("changes") or []), "next_page_token": body.get("nextPageToken"),
                "new_start_page_token": body.get("newStartPageToken")}

    async def search_folders(self, name: str | None = None, *, page_size: int = 50) -> list[dict]:
        q = [f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
        if name:
            q.append(f"name contains '{name.replace(chr(39), chr(92) + chr(39))}'")
        body = await self.api.request("GET", FILES_URL, params={
            "q": " and ".join(q), "fields": LIST_FIELDS, "pageSize": page_size,
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})
        return list(body.get("files") or [])

    # explicitly unsupported: this integration never changes Drive
    async def upload(self, *_a, **_kw):
        raise Unsupported("AZKT never writes to the importer's Drive")

    async def set_permission(self, *_a, **_kw):
        raise Unsupported("AZKT never changes sharing on a source folder")


class FakeDrive:
    """In-memory Drive with a changes feed. `add_folder` / `add_file` build the tree; `move`,
    `revoke`, `new_version` and `trash` drive the incremental-scan scenarios."""

    kind = "fake"

    def __init__(self, owner: str = "dylxnxil@gmail.com"):
        self.owner = owner
        self.files: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.changes: list[dict] = []
        self._token = 1
        self.denied: set[str] = set()
        self.downloads: list[str] = []
        self.offline = False
        self.fail_download_once: set[str] = set()

    # ── construction helpers ─────────────────────────────────────────────
    def _record_change(self, file_id: str, *, removed: bool = False) -> None:
        self._token += 1
        self.changes.append({"fileId": file_id, "removed": removed, "time": f"2026-09-15T00:00:{self._token:02d}Z",
                             "file": None if removed else dict(self.files.get(file_id) or {}), "_token": self._token})

    def add_folder(self, file_id: str, name: str, parent: str | None = None, *, owner: str | None = None) -> dict:
        meta = {"id": file_id, "name": name, "mimeType": FOLDER_MIME, "parents": [parent] if parent else [],
                "modifiedTime": "2026-09-01T00:00:00Z", "createdTime": "2026-09-01T00:00:00Z", "version": "1",
                "trashed": False, "owners": [{"displayName": "Dylan", "emailAddress": owner or self.owner}],
                "webViewLink": f"https://drive.google.com/drive/folders/{file_id}",
                "capabilities": {"canDownload": True}}
        self.files[file_id] = meta
        self._record_change(file_id)
        return meta

    def add_file(self, file_id: str, name: str, parent: str, data: bytes, *, mime: str = "image/jpeg",
                 modified: str = "2026-09-10T00:00:00Z", owner: str | None = None, version: str = "1") -> dict:
        meta = {"id": file_id, "name": name, "mimeType": mime, "parents": [parent],
                "modifiedTime": modified, "createdTime": modified, "version": version, "trashed": False,
                "md5Checksum": hashlib.md5(data).hexdigest(), "size": str(len(data)),
                "owners": [{"displayName": "Importer", "emailAddress": owner or self.owner}],
                "webViewLink": f"https://drive.google.com/file/d/{file_id}/view",
                "capabilities": {"canDownload": True}}
        self.files[file_id] = meta
        self.blobs[file_id] = data
        self._record_change(file_id)
        return meta

    def new_version(self, file_id: str, data: bytes, *, modified: str = "2026-09-12T00:00:00Z") -> dict:
        meta = self.files[file_id]
        meta["md5Checksum"] = hashlib.md5(data).hexdigest()
        meta["size"] = str(len(data))
        meta["version"] = str(int(meta.get("version", "1")) + 1)
        meta["modifiedTime"] = modified
        self.blobs[file_id] = data
        self._record_change(file_id)
        return meta

    def move(self, file_id: str, new_parent: str) -> dict:
        meta = self.files[file_id]
        meta["parents"] = [new_parent]
        self._record_change(file_id)
        return meta

    def trash(self, file_id: str) -> None:
        self.files[file_id]["trashed"] = True
        self._record_change(file_id, removed=True)

    def revoke(self, *file_ids: str) -> None:
        """Access removed at the source: later reads raise permission_denied (F03)."""
        self.denied.update(file_ids)

    # ── adapter surface ──────────────────────────────────────────────────
    def _guard(self, file_id: str | None = None) -> None:
        if self.offline:
            raise ProviderError("drive auth expired", kind="auth_expired")
        if file_id and file_id in self.denied:
            raise ProviderError(f"drive permission denied for {file_id}", kind="permission_denied")

    async def list_children(self, folder_id: str, page_token: str | None = None, *, page_size: int = 200) -> dict:
        self._guard(folder_id)
        rows = [dict(m) for m in self.files.values()
                if folder_id in (m.get("parents") or []) and not m.get("trashed") and m["id"] not in self.denied]
        rows.sort(key=lambda m: m["name"])
        start = int(page_token or 0)
        page = rows[start:start + page_size]
        nxt = str(start + page_size) if start + page_size < len(rows) else None
        return {"files": page, "next_page_token": nxt}

    async def get_file_meta(self, file_id: str) -> dict:
        self._guard(file_id)
        meta = self.files.get(file_id)
        if meta is None:
            raise ProviderError(f"drive file {file_id} not found", kind="invalid_input")
        return dict(meta)

    async def download(self, file_id: str) -> bytes:
        self._guard(file_id)
        self.downloads.append(file_id)
        if file_id in self.fail_download_once:
            self.fail_download_once.discard(file_id)
            raise ProviderError("drive transient error 503", kind="transient")
        if str(self.files.get(file_id, {}).get("mimeType", "")).startswith(GOOGLE_NATIVE_PREFIX):
            raise Unsupported("Google-native documents are not importable originals")
        data = self.blobs.get(file_id)
        if data is None:
            raise ProviderError(f"drive file {file_id} has no content", kind="invalid_input")
        return data

    async def changes_start_page_token(self) -> str:
        self._guard()
        return str(self._token)

    async def changes_list(self, page_token: str, *, page_size: int = 200) -> dict:
        self._guard()
        after = int(page_token or 0)
        rows = [c for c in self.changes if c["_token"] > after]
        rows.sort(key=lambda c: c["_token"])
        page = rows[:page_size]
        new_token = str(page[-1]["_token"] if page else self._token)
        return {"changes": [{k: v for k, v in c.items() if k != "_token"} for c in page],
                "next_page_token": None, "new_start_page_token": new_token}

    async def search_folders(self, name: str | None = None, *, page_size: int = 50) -> list[dict]:
        self._guard()
        rows = [dict(m) for m in self.files.values()
                if m.get("mimeType") == FOLDER_MIME and not m.get("trashed")
                and (not name or name.lower() in m["name"].lower())]
        rows.sort(key=lambda m: m["name"])
        return rows[:page_size]

    async def upload(self, *_a, **_kw):
        raise Unsupported("AZKT never writes to the importer's Drive")

    async def set_permission(self, *_a, **_kw):
        raise Unsupported("AZKT never changes sharing on a source folder")


# ── factory ──────────────────────────────────────────────────────────────────
_ADAPTER: Any | None = None


def set_adapter(adapter: Any | None) -> None:
    global _ADAPTER
    _ADAPTER = adapter


def current_adapter() -> Any | None:
    return _ADAPTER


def adapter_for(db, conn) -> Any:
    """The active Drive client. `Unsupported` (setup blocked) when Google is not connected."""
    if _ADAPTER is not None:
        return _ADAPTER
    if conn is None or conn.status == "disconnected":
        raise Unsupported("Google Drive is not connected; connect it under Settings → Connections",
                          setup_blocked="drive.oauth")
    from ..core.config import settings
    if settings.ENV == "test":
        raise Unsupported("live Drive calls are disabled in tests; install a fixture adapter")
    from .google_oauth import GoogleApi
    assert_read_only(list(conn.granted_scopes or []))
    return DriveAdapter(GoogleApi(db, conn))
