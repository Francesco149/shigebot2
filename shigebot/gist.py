"""
shigebot/gist.py — script source manager.

Supports two source kinds:

  Gist:    "https://gist.github.com/owner/ID"
  GitHub:  "github:owner/repo:path/to/file.py"
           "github:owner/repo:path/to/file.py@ref"   (specific branch/tag/SHA)

The GitHub Contents API is used for both kinds so rate-limit headers are
consistent. With GITHUB_TOKEN the limit is 5000 req/hour; without it 60/hour.

Freshness tracking:
  Gist:   updated_at timestamp (cheap check; skip fetch if unchanged)
  GitHub: file blob SHA (same idea)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import os
from pathlib import Path

import httpx

from .names import name_to_filename

logger = logging.getLogger(__name__)


# ── URL parsing ────────────────────────────────────────────────────────────

_GIST_RE = re.compile(
    r"gist\.github(?:usercontent)?\.com/([^/]+)/([a-f0-9]+)",
    re.IGNORECASE,
)
_GITHUB_FILE_RE = re.compile(
    r"^github:([^/]+)/([^:]+):(.+?)(?:@([^@]+))?$"
)


def _parse_gist_url(url: str) -> tuple[str, str]:
    m = _GIST_RE.search(url)
    if not m:
        raise ValueError(f"Cannot parse gist URL: {url!r}")
    return m.group(1), m.group(2)


def _parse_github_file(url: str) -> dict:
    """
    Parse "github:owner/repo:path/to/file.py[@ref]".
    Returns {"owner", "repo", "path", "ref"} (ref defaults to "HEAD").
    """
    m = _GITHUB_FILE_RE.match(url)
    if not m:
        raise ValueError(f"Cannot parse github file URL: {url!r}")
    return {
        "owner": m.group(1),
        "repo":  m.group(2),
        "path":  m.group(3),
        "ref":   m.group(4) or "HEAD",
    }


def is_github_file(url: str) -> bool:
    return url.startswith("github:")


# ── GistManager ───────────────────────────────────────────────────────────

class GistManager:
    def __init__(
        self,
        scripts: dict[str, str],
        working_dir: Path,
        refresh_interval: int = 300,
    ) -> None:
        self.scripts          = scripts
        self.working_dir      = working_dir
        self.refresh_interval = refresh_interval
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

        # Freshness caches
        self._gist_updated_at: dict[str, str] = {}   # name → updated_at
        self._github_shas:     dict[str, str] = {}   # name → blob sha

    async def __aenter__(self) -> "GistManager":
        headers = {
            "User-Agent": "shigebot/1.0",
            "Accept":     "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            logger.info("GitHub API: authenticated (5000 req/hour)")
        else:
            logger.info("GitHub API: unauthenticated (60 req/hour) — set GITHUB_TOKEN")

        self._client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers=headers,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Path helpers ───────────────────────────────────────────────────────

    def script_path(self, name: str) -> Path:
        return self.working_dir / name_to_filename(name)

    def script_exists(self, name: str) -> bool:
        return self.script_path(name).exists()

    # ── Public fetch API ───────────────────────────────────────────────────

    async def fetch_one(self, name: str, url: str) -> bool:
        """
        Fetch/refresh a single script. Returns True if the file was written.
        Routes to the appropriate backend based on the URL format.
        """
        assert self._client is not None, "Must be used as async context manager"

        try:
            if is_github_file(url):
                return await self._fetch_github_file(name, url)
            else:
                return await self._fetch_gist(name, url)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Unexpected error fetching %r: %s", name, exc, exc_info=True)
            return False

    async def fetch_all(self) -> dict[str, bool]:
        """Fetch all scripts concurrently. Returns {name: updated}."""
        tasks = [
            asyncio.create_task(self.fetch_one(name, url), name=f"fetch:{name}")
            for name, url in self.scripts.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, bool] = {}
        for name, result in zip(self.scripts, results):
            if isinstance(result, Exception):
                logger.error("Error fetching %r: %s", name, result)
                out[name] = False
            else:
                out[name] = result  # type: ignore[assignment]
        return out

    async def refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.refresh_interval)
                logger.info("Polling sources for updates…")
                await self.fetch_all()
            except asyncio.CancelledError:
                logger.info("Gist refresh loop cancelled")
                raise
            except Exception as exc:
                logger.error("Error in refresh loop: %s", exc, exc_info=True)

    # ── Gist backend ───────────────────────────────────────────────────────

    async def _fetch_gist(self, name: str, url: str) -> bool:
        try:
            _, gist_id = _parse_gist_url(url)
        except ValueError as exc:
            logger.error("Bad gist URL for %r: %s", name, exc)
            return False

        api_url = f"https://api.github.com/gists/{gist_id}"
        try:
            resp = await self._client.get(api_url)   # type: ignore[union-attr]
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s fetching gist %r", exc.response.status_code, name)
            return False
        except httpx.RequestError as exc:
            logger.error("Network error fetching gist %r: %s", name, exc)
            return False

        data       = resp.json()
        updated_at = data.get("updated_at", "")
        path       = self.script_path(name)

        if updated_at and self._gist_updated_at.get(name) == updated_at and path.exists():
            logger.debug("Gist %r unchanged (%s)", name, updated_at)
            return False

        files   = data.get("files", {})
        py_files = {k: v for k, v in files.items() if k.endswith(".py")}
        if not py_files:
            logger.error("Gist %r: no .py file found", name)
            return False

        file_data = next(iter(py_files.values()))
        content   = file_data.get("content")

        if content is None:
            raw_url = file_data.get("raw_url")
            if not raw_url:
                logger.error("Gist %r: no content or raw_url", name)
                return False
            try:
                raw = await self._client.get(raw_url)   # type: ignore[union-attr]
                raw.raise_for_status()
                content = raw.text
            except Exception as exc:
                logger.error("Error fetching truncated gist %r: %s", name, exc)
                return False

        written = await self._write_if_changed(name, path, content)
        if written:
            self._gist_updated_at[name] = updated_at
        elif updated_at:
            # Metadata changed but content identical — still update cache tag
            self._gist_updated_at[name] = updated_at
        return written

    # ── GitHub file backend ────────────────────────────────────────────────

    async def _fetch_github_file(self, name: str, url: str) -> bool:
        try:
            parts = _parse_github_file(url)
        except ValueError as exc:
            logger.error("Bad github file URL for %r: %s", name, exc)
            return False

        api_url = (
            f"https://api.github.com/repos/{parts['owner']}/{parts['repo']}"
            f"/contents/{parts['path']}?ref={parts['ref']}"
        )
        try:
            resp = await self._client.get(api_url)   # type: ignore[union-attr]
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s fetching github file %r", exc.response.status_code, name)
            return False
        except httpx.RequestError as exc:
            logger.error("Network error fetching github file %r: %s", name, exc)
            return False

        data    = resp.json()
        sha     = data.get("sha", "")
        path    = self.script_path(name)

        if sha and self._github_shas.get(name) == sha and path.exists():
            logger.debug("GitHub file %r unchanged (sha=%s)", name, sha[:7])
            return False

        encoding = data.get("encoding", "")
        raw_content = data.get("content", "")

        if encoding == "base64":
            content = base64.b64decode(raw_content).decode("utf-8", errors="replace")
        elif encoding == "none" or not encoding:
            # Large files: GitHub omits content and provides a download_url
            download_url = data.get("download_url")
            if not download_url:
                logger.error("GitHub file %r: no content and no download_url", name)
                return False
            try:
                dl = await self._client.get(download_url)   # type: ignore[union-attr]
                dl.raise_for_status()
                content = dl.text
            except Exception as exc:
                logger.error("Error downloading github file %r: %s", name, exc)
                return False
        else:
            logger.error("GitHub file %r: unsupported encoding %r", name, encoding)
            return False

        written = await self._write_if_changed(name, path, content)
        if sha:
            self._github_shas[name] = sha
        return written

    # ── Shared write helper ────────────────────────────────────────────────

    async def _write_if_changed(self, name: str, path: Path, content: str) -> bool:
        """Write `content` to `path` if it differs. Returns True if written."""
        path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            if existing == content:
                logger.debug("Script %r: content unchanged", name)
                return False
            path.write_text(content, encoding="utf-8")

        if existing is None:
            logger.info("Script %r downloaded to %s", name, path)
        else:
            logger.info("Script %r updated at %s", name, path)
        return True
