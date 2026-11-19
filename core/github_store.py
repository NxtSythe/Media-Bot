"""Keep the database in a GitHub repository.

The database (and the last 7 daily backups) live on a separate branch
(default `bot-data`). Every save replaces that branch with ONE fresh commit,
so the repository never grows, no matter how often the bot saves.

If DB_KEY is set, files are encrypted (Fernet / AES) before upload, so the
data stays private even in a public repository. Without the key the data
can't be read - keep it safe.

Environment variables:
  GITHUB_TOKEN       token with `contents: write` (set automatically in GitHub Actions)
  GITHUB_REPOSITORY  owner/repo (set automatically in GitHub Actions)
  DB_KEY             encryption password (strongly recommended, required for public repos)
  DATA_BRANCH        optional, default "bot-data"
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

import aiohttp

log = logging.getLogger("github_store")

API = "https://api.github.com"


class StoreError(Exception):
    pass


def _fernet(password: str):
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(password.encode("utf-8")).digest())
    return Fernet(key)


class GitHubStore:
    def __init__(self, session: aiohttp.ClientSession, repo: str | None = None, token: str | None = None,
                 branch: str | None = None, password: str | None = None):
        self.session = session
        self.repo = repo if repo is not None else os.environ.get("GITHUB_REPOSITORY", "").strip()
        self.token = token if token is not None else (os.environ.get("BOT_GITHUB_TOKEN")
                                                      or os.environ.get("GITHUB_TOKEN", "")).strip()
        self.branch = branch or os.environ.get("DATA_BRANCH", "").strip() or "bot-data"
        password = password if password is not None else os.environ.get("DB_KEY", "")
        self.fernet = _fernet(password) if password else None
        self.suffix = ".enc" if self.fernet else ""
        self.is_public: bool | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.repo and self.token)

    def describe(self) -> str:
        enc = "encrypted" if self.fernet else "NOT encrypted"
        return f"GitHub repo {self.repo}, branch {self.branch} ({enc})"

    # -------------------------------------------------------------- http
    async def _req(self, method: str, path: str, *, json=None, raw: bool = False, ok=(200, 201)):
        headers = {"Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28",
                   "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}
        async with self.session.request(method, API + path, headers=headers, json=json,
                                        timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status == 404:
                return None
            if r.status not in ok:
                text = (await r.text())[:300]
                raise StoreError(f"GitHub API {method} {path} -> HTTP {r.status}: {text}")
            return await r.read() if raw else await r.json()

    async def check(self) -> None:
        """Make sure the repo is reachable; refuse to store plain data in a public repo."""
        info = await self._req("GET", f"/repos/{self.repo}")
        if info is None:
            raise StoreError(f"Repository {self.repo} was not found or the token can't access it.")
        self.is_public = not info.get("private", False)
        if self.is_public and not self.fernet:
            raise StoreError("This repository is public, so the database must be encrypted. "
                             "Add a DB_KEY secret (any long password) and restart.")

    # -------------------------------------------------------------- crypto
    def _encode(self, data: bytes) -> bytes:
        return self.fernet.encrypt(data) if self.fernet else data

    def _decode(self, data: bytes) -> bytes:
        if not self.fernet:
            return data
        from cryptography.fernet import InvalidToken
        try:
            return self.fernet.decrypt(data)
        except InvalidToken:
            raise StoreError("Couldn't decrypt the saved database: DB_KEY is wrong (it must be exactly "
                             "the same password that was used before).")

    # -------------------------------------------------------------- download
    async def download(self, dest: str | Path) -> bool:
        """Download the saved database into `dest`. Returns False if nothing is saved yet."""
        data = await self._req("GET", f"/repos/{self.repo}/contents/data.db{self.suffix}?ref={self.branch}",
                               raw=True)
        if data is None:
            return False
        plain = self._decode(data)
        if not plain.startswith(b"SQLite format 3"):
            raise StoreError("The saved database file is damaged.")
        Path(dest).write_bytes(plain)
        return True

    # -------------------------------------------------------------- upload
    async def upload(self, db_file: str | Path, backup_file: str | Path | None = None, keep_backups: int = 7,
                     message: str = "Update bot data") -> None:
        """Replace the data branch with a single commit holding the database (+ backups)."""
        repo = f"/repos/{self.repo}"
        ref = await self._req("GET", f"{repo}/git/ref/heads/{self.branch}")
        base_tree, existing = None, []
        if ref:
            commit = await self._req("GET", f"{repo}/git/commits/{ref['object']['sha']}")
            base_tree = commit["tree"]["sha"]
            tree = await self._req("GET", f"{repo}/git/trees/{base_tree}?recursive=1") or {}
            existing = [t["path"] for t in tree.get("tree", []) if t.get("type") == "blob"]

        entries = [await self._blob_entry(f"data.db{self.suffix}", Path(db_file).read_bytes())]
        if backup_file:
            name = f"backups/{Path(backup_file).name}{self.suffix}"
            entries.append(await self._blob_entry(name, Path(backup_file).read_bytes()))
            backups = sorted({p for p in existing if p.startswith("backups/")} | {name})
            for old in backups[:-keep_backups]:
                entries.append({"path": old, "mode": "100644", "type": "blob", "sha": None})
        readme = ("This branch is written by the LunaMC Media Bot and holds its database.\n"
                  "Do not edit it by hand.\n")
        if "README.md" not in existing:
            entries.append(await self._blob_entry("README.md", readme.encode(), encode=False))

        body = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        new_tree = await self._req("POST", f"{repo}/git/trees", json=body)
        new_commit = await self._req("POST", f"{repo}/git/commits",
                                     json={"message": message, "tree": new_tree["sha"], "parents": []})
        if ref:
            await self._req("PATCH", f"{repo}/git/refs/heads/{self.branch}",
                            json={"sha": new_commit["sha"], "force": True})
        else:
            await self._req("POST", f"{repo}/git/refs",
                            json={"ref": f"refs/heads/{self.branch}", "sha": new_commit["sha"]})

    async def _blob_entry(self, path: str, data: bytes, encode: bool = True) -> dict:
        content = self._encode(data) if encode else data
        blob = await self._req("POST", f"/repos/{self.repo}/git/blobs",
                               json={"content": base64.b64encode(content).decode(), "encoding": "base64"})
        return {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
