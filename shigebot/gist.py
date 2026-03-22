"""
GistManager: fetches community scripts from GitHub Gists,
writes them to disk, and polls for updates on a timer.

We use the GitHub Gist API (api.github.com/gists/ID) rather than the
raw content CDN (gist.githubusercontent.com). The CDN aggressively
caches /raw responses for several minutes regardless of ETag or
cache-busting headers, making it useless for detecting recent edits.

The API returns a JSON object that includes:
  - updated_at: ISO timestamp of the last edit
  - files: {filename: {content: "..."}}

We check updated_at first (cheap) and only extract the file content
when it has changed. With a GitHub token in GITHUB_TOKEN the rate
limit is 5000 req/hour; unauthenticated is 60 req/hour (fine for
a handful of scripts polled every few minutes).
"""
from __future__ import annotations

import asyncio
import logging
import re
import os
from pathlib import Path

import httpx

from .names import name_to_filename

logger = logging.getLogger(__name__)

_GIST_RE = re.compile(
    r"gist\.github(?:usercontent)?\.com/([^/]+)/([a-f0-9]+)",
    re.IGNORECASE,
)


def _parse_gist_url(url: str) -> tuple[str, str]:
    m = _GIST_RE.search(url)
    if not m:
        raise ValueError(f"Cannot parse gist URL: {url!r}")
    return m.group(1), m.group(2)


def _api_url(gist_id: str) -> str:
    return f"https://api.github.com/gists/{gist_id}"


class GistManager:
    def __init__(
        self,
        scripts: dict[str, str],
        working_dir: Path,
        refresh_interval: int = 300,
    ) -> None:
        self.scripts = scripts
        self.working_dir = working_dir
        self.refresh_interval = refresh_interval
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Cache updated_at timestamps to skip unchanged gists cheaply
        self._updated_at: dict[str, str] = {}

    async def __aenter__(self) -> "GistManager":
        headers = {
            "User-Agent": "shigebot/1.0",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
            logger.info("GitHub API: using authenticated requests (5000 req/hour)")
        else:
            logger.info("GitHub API: unauthenticated (60 req/hour) — set GITHUB_TOKEN for higher limits")

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

    def script_path(self, name: str) -> Path:
        return self.working_dir / name_to_filename(name)

    def script_exists(self, name: str) -> bool:
        return self.script_path(name).exists()

    async def fetch_one(self, name: str, url: str) -> bool:
        """
        Fetch/refresh a single script via the GitHub Gist API.

        Checks updated_at first — if unchanged and the file exists on disk,
        skips the content extraction. Returns True if the file was written.
        """
        assert self._client is not None, "Must be used as async context manager"

        try:
            _, gist_id = _parse_gist_url(url)
        except ValueError as exc:
            logger.error("Bad gist URL for script %r: %s", name, exc)
            return False

        try:
            resp = await self._client.get(_api_url(gist_id))
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s fetching script %r", exc.response.status_code, name)
            return False
        except httpx.RequestError as exc:
            logger.error("Network error fetching script %r: %s", name, exc)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Unexpected error fetching script %r: %s", name, exc, exc_info=True)
            return False

        data = resp.json()
        updated_at = data.get("updated_at", "")
        path = self.script_path(name)

        # Skip if unchanged since last fetch and file already exists
        if updated_at and self._updated_at.get(name) == updated_at and path.exists():
            logger.debug("Script %r unchanged (updated_at=%s)", name, updated_at)
            return False

        # Extract the first .py file from the gist
        files = data.get("files", {})
        py_files = {k: v for k, v in files.items() if k.endswith(".py")}
        if not py_files:
            logger.error("Script %r: no .py file found in gist %s", name, gist_id)
            return False

        file_data = next(iter(py_files.values()))
        content = file_data.get("content")

        if content is None:
            # File is truncated — fetch the raw URL the API provides
            raw_url = file_data.get("raw_url")
            if not raw_url:
                logger.error("Script %r: no content or raw_url in gist response", name)
                return False
            try:
                raw_resp = await self._client.get(raw_url)
                raw_resp.raise_for_status()
                content = raw_resp.text
            except Exception as exc:
                logger.error("Error fetching truncated content for script %r: %s", name, exc)
                return False

        path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            existing = path.read_text(encoding="utf-8") if path.exists() else None
            if existing == content:
                # updated_at changed but content didn't (e.g. description edit)
                self._updated_at[name] = updated_at
                logger.debug("Script %r: metadata changed but content unchanged", name)
                return False
            path.write_text(content, encoding="utf-8")

        self._updated_at[name] = updated_at
        if existing is None:
            logger.info("Script %r downloaded to %s", name, path)
        else:
            logger.info("Script %r updated at %s", name, path)
        return True

    async def fetch_all(self) -> dict[str, bool]:
        """Fetch all scripts concurrently. Returns {name: updated} for each."""
        tasks = [
            asyncio.create_task(self.fetch_one(name, url), name=f"fetch:{name}")
            for name, url in self.scripts.items()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: dict[str, bool] = {}
        for name, result in zip(self.scripts, results):
            if isinstance(result, Exception):
                logger.error("Unexpected error fetching script %r: %s", name, result)
                out[name] = False
            else:
                out[name] = result
        return out

    async def refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.refresh_interval)
                logger.info("Polling gists for updates…")
                await self.fetch_all()
            except asyncio.CancelledError:
                logger.info("Gist refresh loop cancelled")
                raise
            except Exception as exc:
                logger.error("Unexpected error in gist refresh loop: %s", exc, exc_info=True)
