# AI Context Summarize -> Github Draft Issue

# Will Make issue in the TRD_LineLog Repo and later can transfer issue
import logging
from urllib.parse import quote

import aiohttp

from app.core.config import settings

logger = logging.getLogger(__name__)


class GithubClientError(Exception):
    pass


def _headers(token: str | None = None) -> dict:
    return {
        "Authorization": f"Bearer {token or settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TRD-LineLog",
    }


def _repo_url() -> str:
    return f"{settings.github_api_base}/repos/{settings.github_owner}/{settings.github_repo}"


# GitHub has no API for issue attachments — the drag-and-drop uploader on
# github.com is a private web flow. Release assets are the closest supported
# thing: any binary, no git history bloat, and on a private repo the download
# URLs are gated by the same repo permissions as the issue itself. One
# long-lived release acts as the bucket; assets are added to it forever.
_ASSETS_RELEASE_BODY = (
    "Files captured from LINE conversations and linked from auto-generated "
    "issues. This is a storage bucket, not a software release."
)

# Cached for the life of the process: every job would otherwise re-resolve the
# same release before uploading its first file.
_assets_release: tuple[int, str] | None = None


async def _get_assets_release(session: aiohttp.ClientSession) -> tuple[int, str] | None:
    url = f"{_repo_url()}/releases/tags/{quote(settings.github_assets_tag)}"
    async with session.get(url, headers=_headers()) as resp:
        if resp.status == 404:
            return None
        data = await resp.json(content_type=None)
        if resp.status in (401, 403):
            raise GithubClientError(
                f"GitHub {resp.status}: {(data or {}).get('message', data)} — uploading "
                "attachments needs 'Contents: Read and write' on the token, which is a "
                "wider scope than creating issues. Add it, or set ATTACH_FILES_TO_ISSUES=false."
            )
        if resp.status != 200:
            raise GithubClientError(f"Could not read assets release ({resp.status}): {(data or {}).get('message', data)}")
        # upload_url is a URI template ("...assets{?name,label}") — the query
        # part is ours to fill in, so drop the placeholder.
        return data["id"], data["upload_url"].split("{")[0]


async def _create_assets_release(session: aiohttp.ClientSession) -> tuple[int, str]:
    payload = {
        "tag_name": settings.github_assets_tag,
        "name": "LINE attachments",
        "body": _ASSETS_RELEASE_BODY,
        # Keep it off the repo's "Latest release" badge — it isn't one.
        "make_latest": "false",
    }
    async with session.post(f"{_repo_url()}/releases", headers=_headers(), json=payload) as resp:
        data = await resp.json(content_type=None)
        if resp.status == 201:
            return data["id"], data["upload_url"].split("{")[0]
        if resp.status == 422:
            # Another process created it between our GET and POST.
            existing = await _get_assets_release(session)
            if existing is not None:
                return existing
        message = (data or {}).get("message", data)
        if resp.status in (401, 403):
            raise GithubClientError(
                f"GitHub {resp.status}: {message} — uploading attachments needs "
                f"'Contents: Read and write' on {settings.github_owner}/{settings.github_repo}, "
                "which is a wider scope than creating issues."
            )
        raise GithubClientError(f"Could not create assets release ({resp.status}): {message}")


async def _ensure_assets_release(session: aiohttp.ClientSession) -> tuple[int, str]:
    global _assets_release
    if _assets_release is None:
        _assets_release = await _get_assets_release(session) or await _create_assets_release(session)
    return _assets_release


async def _find_asset_url(session: aiohttp.ClientSession, release_id: int, name: str) -> str | None:
    """Locates an asset already uploaded under this name. Only reachable when
    a job is redelivered after its upload succeeded but before the issue was
    filed — re-uploading would 422 forever otherwise."""
    page = 1
    while page <= 10:
        url = f"{_repo_url()}/releases/{release_id}/assets?per_page=100&page={page}"
        async with session.get(url, headers=_headers()) as resp:
            if resp.status != 200:
                return None
            assets = await resp.json(content_type=None)
        if not assets:
            return None
        for asset in assets:
            if asset.get("name") == name:
                return asset["browser_download_url"]
        page += 1
    return None


async def upload_release_asset(name: str, data: bytes, mime: str) -> str:
    """Uploads one file and returns its browser_download_url. On a private
    repo that URL requires the viewer to be signed in with repo access —
    which is the desired behaviour for support material."""
    timeout = aiohttp.ClientTimeout(total=settings.github_upload_timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        release_id, upload_url = await _ensure_assets_release(session)

        headers = {**_headers(), "Content-Type": mime}
        async with session.post(f"{upload_url}?name={quote(name)}", headers=headers, data=data) as resp:
            payload = await resp.json(content_type=None)
            if resp.status == 201:
                return payload["browser_download_url"]

            if resp.status == 422:
                existing = await _find_asset_url(session, release_id, name)
                if existing is not None:
                    logger.info("Asset %s was already uploaded — reusing it", name)
                    return existing

            message = (payload or {}).get("message", payload)
            raise GithubClientError(f"Asset upload failed for {name} ({resp.status}): {message}")


async def create_issue(
    title: str, body: str, labels: list[str] | None = None, token: str | None = None
) -> tuple[int, str]:
    """
    Create Issue On Github LineLog Repo (settings.github_owner/settings.github_repo)
    Return (issue) number, html_url

    `token`, when given, overrides settings.github_token — used to file the
    issue under the reporter's own mapped GitHub account (see
    settings.github_token_by_name) instead of the default token.
    """
    url = f"{settings.github_api_base}/repos/{settings.github_owner}/{settings.github_repo}/issues"
    headers = _headers(token)
    payload = {"title": title, "body": body, "labels": labels if labels is not None else settings.github_labels}

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session: # Conn Pool, It will Close itself
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json(content_type=None)

            if resp.status == 201:
                return data["number"], data["html_url"]

            message = (data or {}).get("message", data)

            if resp.status in (401, 403):
                logger.error(
                    "GitHub auth/permission error %s: %s (retry-after=%s, rate-remaining=%s)",
                    resp.status,
                    message,
                    resp.headers.get("Retry-After"),
                    resp.headers.get("x-ratelimit-remaining"),
                )
                raise GithubClientError(
                    f"GitHub {resp.status}: {message} — check GITHUB_TOKEN scope "
                    f"(needs Issues: Read and write on {settings.github_owner}/{settings.github_repo})"
                )
            if resp.status == 404:
                raise GithubClientError(
                    f"Repo not found or token lacks access: {settings.github_owner}/{settings.github_repo}"
                )
            if resp.status == 429:
                raise GithubClientError(f"GitHub rate limited: {message} (retry-after={resp.headers.get('Retry-After')})")

            raise GithubClientError(f"GitHub issue creation failed ({resp.status}): {message}")