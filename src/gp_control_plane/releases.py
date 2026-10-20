from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


REPO_URL = "https://github.com/balbomush/GP-plane.git"
RELEASES_API_URL = "https://api.github.com/repos/balbomush/GP-plane/releases"
RELEASES_PAGE_URL = "https://github.com/balbomush/GP-plane/releases"
LATEST_RELEASE_URL = "https://github.com/balbomush/GP-plane/releases/latest"
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([A-Za-z0-9.-]+))?")


def release_channel_info(
    *,
    current_version: str,
    channel: str,
    fetcher: Callable[[], str] | None = None,
    tag_fetcher: Callable[[], str] | None = None,
) -> dict[str, Any]:
    clean_channel = channel if channel in {"stable", "prerelease"} else "stable"
    errors: list[dict[str, str]] = []
    try:
        raw = fetcher() if fetcher else fetch_github_releases()
        releases = parse_github_releases(raw)
        selected = _select_release(releases, clean_channel)
        if not selected:
            raise ValueError("no release found for selected channel")
        available_version = str(selected.get("tag_name") or "").strip()
        release_url = str(selected.get("html_url") or RELEASES_PAGE_URL)
        return {
            "channel": clean_channel,
            "current_version": current_version,
            "available_version": available_version or "-",
            "available_name": str(selected.get("name") or available_version or ""),
            "body": str(selected.get("body") or ""),
            "assets": _release_assets(selected),
            "published_at": str(selected.get("published_at") or ""),
            "url": release_url,
            "checked": True,
            "source": "github",
            "update_available": _version_key(available_version) > _version_key(current_version),
            "error": "",
            "error_kind": "",
            "error_stage": "",
            "errors": [],
        }
    except Exception as api_exc:  # noqa: BLE001
        errors.append(_release_error("github_api", api_exc))
        try:
            raw_tags = tag_fetcher() if tag_fetcher else fetch_git_tags()
            selected_tag = _select_git_tag(parse_git_tags(raw_tags), clean_channel)
            if not selected_tag:
                raise ValueError("no release tag found for selected channel")
            return {
                "channel": clean_channel,
                "current_version": current_version,
                "available_version": selected_tag,
                "available_name": selected_tag,
                "body": "",
                "assets": [],
                "published_at": "",
                "url": f"{RELEASES_PAGE_URL}/tag/{selected_tag}",
                "checked": True,
                "source": "git-tags",
                "update_available": _version_key(selected_tag) > _version_key(current_version),
                "error": str(api_exc),
                "error_kind": errors[0]["kind"],
                "error_stage": errors[0]["stage"],
                "errors": errors,
            }
        except Exception as tag_exc:  # noqa: BLE001
            errors.append(_release_error("git_tags", tag_exc))
            return {
                "channel": clean_channel,
                "current_version": current_version,
                "available_version": "-",
                "available_name": "",
                "body": "",
                "assets": [],
                "published_at": "",
                "url": LATEST_RELEASE_URL if clean_channel == "stable" else RELEASES_PAGE_URL,
                "checked": False,
                "source": "fallback",
                "update_available": False,
                "error": f"{api_exc}; git tag fallback failed: {tag_exc}",
                "error_kind": errors[0]["kind"],
                "error_stage": errors[0]["stage"],
                "errors": errors,
            }


def fetch_github_releases() -> str:
    request = Request(RELEASES_API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "gp-control-plane"})
    with urlopen(request, timeout=20) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def fetch_git_tags() -> str:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", REPO_URL],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


def parse_github_releases(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError("invalid GitHub releases response") from exc
    if not isinstance(payload, list):
        raise ValueError("invalid GitHub releases response")
    releases: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, dict) and not item.get("draft"):
            releases.append(item)
    return releases


def parse_git_tags(text: str) -> list[str]:
    tags: list[str] = []
    for raw_line in str(text or "").splitlines():
        parts = raw_line.strip().split()
        if len(parts) < 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/").strip()
        if tag:
            tags.append(tag)
    return tags


def _select_release(releases: list[dict[str, Any]], channel: str) -> dict[str, Any] | None:
    for release in releases:
        prerelease = bool(release.get("prerelease"))
        if channel == "prerelease" and prerelease:
            return release
        if channel == "stable" and not prerelease:
            return release
    return None


def _select_git_tag(tags: list[str], channel: str) -> str:
    candidates = []
    for tag in tags:
        prerelease = _is_prerelease_tag(tag)
        if channel == "stable" and prerelease:
            continue
        if channel == "prerelease" and not prerelease:
            continue
        if _version_tuple(tag) == (0, 0, 0):
            continue
        candidates.append(tag)
    if not candidates:
        return ""
    return sorted(candidates, key=_tag_sort_key, reverse=True)[0]


def _is_prerelease_tag(tag: str) -> bool:
    return "-" in str(tag or "").strip()


def _tag_sort_key(tag: str) -> tuple[VersionKey, str]:
    return (_version_key(tag), str(tag or ""))


def _release_assets(release: dict[str, Any]) -> list[dict[str, str]]:
    assets = release.get("assets") if isinstance(release.get("assets"), list) else []
    result: list[dict[str, str]] = []
    for item in assets:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "name": str(item.get("name") or ""),
                "url": str(item.get("browser_download_url") or ""),
                "size": str(item.get("size") or ""),
            }
        )
    return result


def _release_error(stage: str, exc: BaseException) -> dict[str, str]:
    return {
        "stage": stage,
        "kind": _release_error_kind(exc),
        "message": str(exc) or exc.__class__.__name__,
    }


def _release_error_kind(exc: BaseException) -> str:
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "format"
    if isinstance(exc, subprocess.SubprocessError):
        return "git"
    if isinstance(exc, (OSError, TimeoutError, URLError)):
        return "network"
    return "unexpected"


VersionKey = tuple[tuple[int, int, int], int, tuple[tuple[int, int | str], ...]]


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(str(value or "").strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups()[:3])


def _version_key(value: str) -> VersionKey:
    match = _VERSION_RE.match(str(value or "").strip())
    if not match:
        return ((0, 0, 0), -1, ())
    numeric = tuple(int(part or 0) for part in match.groups()[:3])
    suffix = str(match.group(4) or "")
    if not suffix:
        return (numeric, 1, ())
    return (numeric, 0, _prerelease_key(suffix))


def _prerelease_key(value: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for raw_part in re.split(r"[.-]+", value):
        part = raw_part.strip()
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part.lower()))
    return tuple(parts)
