#!/usr/bin/env python3
"""Generate recent public commit cards for a GitHub profile README.

The collector uses GitHub's commit search rather than public PushEvent payloads.
Commit search can verify the linked GitHub author with far fewer requests, but it
only indexes commits reachable from a repository's default branch.  See
docs/recent-commits.md for the operational limits.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "recent-commits.config.json"
DEFAULT_README_PATH = ROOT / "README.md"
DEFAULT_OUTPUT_DIR = ROOT / "assets" / "recent-commits"
DEFAULT_PREVIEW_DIR = ROOT / "preview" / "recent-commits"

START_MARKER = "<!-- RECENT_COMMITS_START -->"
END_MARKER = "<!-- RECENT_COMMITS_END -->"
SLOT_COUNT = 3

SUPPORTED_TYPES = (
    "feat",
    "fix",
    "refactor",
    "docs",
    "test",
    "perf",
    "chore",
    "build",
    "ci",
    "style",
    "revert",
)
CONVENTIONAL_RE = re.compile(
    r"^\s*(" + "|".join(SUPPORTED_TYPES) + r")"
    r"(?:\([^)\r\n]+\))?!?:\s*(.+?)\s*$",
    re.IGNORECASE,
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

DEFAULT_EXCLUDE_PATTERNS = (
    r"^Merge pull request",
    r"^Update README\.md$",
    r"update recent commits",
    r"update recent commit cards",
)

TYPE_COLORS = {
    "FEAT": "#FF6685",
    "FIX": "#42C7C9",
    "REFACTOR": "#8B6CF6",
    "DOCS": "#FFC94A",
    "CHORE": "#8C959F",
    "TEST": "#75B9F4",
    "PERF": "#FF9F68",
    "BUILD": "#75D3A6",
    "CI": "#6FA8F7",
    "STYLE": "#E69AD8",
    "REVERT": "#F28B82",
    "COMMIT": "#C6B5E8",
}


class GeneratorError(RuntimeError):
    """Base error for a safe, non-writing generator failure."""


class GitHubAPIError(GeneratorError):
    """GitHub returned an error or a response that cannot be trusted."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SearchNeedsSplit(GeneratorError):
    """A search interval must be split to remain complete."""


@dataclass(frozen=True)
class Commit:
    sha: str
    repository: str
    message: str
    title: str
    commit_type: str
    authored_at: datetime
    url: str
    language: str
    author_login: str
    author_name: str
    author_email: str
    committer_login: str = ""
    committer_name: str = ""
    committer_email: str = ""


def timezone_for(value: str | tzinfo) -> tzinfo:
    """Return a timezone, with a stdlib-only Windows fallback for Seoul."""

    if isinstance(value, tzinfo):
        return value
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        if value == "Asia/Seoul":
            return timezone(timedelta(hours=9), name="Asia/Seoul")
        if value == "UTC":
            return timezone.utc
        raise GeneratorError(
            f"시간대 데이터에서 {value!r}을 찾을 수 없습니다. "
            "Python이 IANA timezone 데이터를 사용할 수 있는지 확인하세요."
        )


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"올바르지 않은 날짜/시간입니다: {value!r}") from exc
    else:
        raise ValueError(f"날짜/시간 값이 없습니다: {value!r}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_commit_message(message: str) -> tuple[str, str]:
    """Return an uppercase conventional type and a readable first-line title."""

    first_line = (message or "").splitlines()[0].strip() if message else ""
    if not first_line:
        return "COMMIT", "제목 없는 커밋"
    match = CONVENTIONAL_RE.match(first_line)
    if not match:
        return "COMMIT", first_line
    title = match.group(2).strip()
    return match.group(1).upper(), title or first_line


def should_exclude_message(message: str, patterns: Sequence[str]) -> bool:
    first_line = (message or "").splitlines()[0].strip()
    return any(re.search(pattern, first_line, re.IGNORECASE) for pattern in patterns)


def is_bot_identity(
    login: str | None, name: str | None, email: str | None
) -> bool:
    """Detect known bot identities without misclassifying names like robotics."""

    values = [value.casefold() for value in (login, name, email) if value]
    known = (
        "dependabot",
        "github-actions",
        "renovate[bot]",
        "renovate-bot",
        "greenkeeper[bot]",
    )
    for value in values:
        if any(token in value for token in known):
            return True
        if value.endswith("[bot]"):
            return True
        if re.search(r"(?:^|[-_.\s])bot(?:$|[-_.\s])", value):
            return True
    return False


def _record_value(record: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(record, Mapping) and name in record:
            return record[name]
        if hasattr(record, name):
            return getattr(record, name)
    return default


def _commit_datetime(record: Any) -> datetime:
    value = _record_value(record, "authored_at", "committed_at", "date")
    return parse_datetime(value)


def _parent_count(record: Any) -> int:
    parents = _record_value(record, "parents", default=None)
    if isinstance(parents, Sequence) and not isinstance(parents, (str, bytes)):
        return len(parents)
    count = _record_value(record, "parent_count", default=0)
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


def normalize_and_filter_commits(
    raw_commits: Iterable[Any], config: Mapping[str, Any]
) -> list[Commit]:
    """Apply all privacy, authorship, bot, message, merge, and SHA filters."""

    username = str(config.get("username", "")).strip()
    excluded_repositories = {
        str(repo).casefold() for repo in config.get("exclude_repositories", [])
    }
    configured_patterns = [
        str(pattern) for pattern in config.get("exclude_message_patterns", [])
    ]
    patterns = list(dict.fromkeys((*DEFAULT_EXCLUDE_PATTERNS, *configured_patterns)))

    filtered: list[Commit] = []
    seen_shas: set[str] = set()

    for raw in raw_commits:
        if bool(_record_value(raw, "private", default=False)):
            continue

        repository_value = _record_value(
            raw, "repository", "repo", "repository_full_name", default=""
        )
        if isinstance(repository_value, Mapping):
            repository = str(
                repository_value.get("full_name") or repository_value.get("name") or ""
            )
        else:
            repository = str(repository_value or "")
        if not REPOSITORY_RE.fullmatch(repository):
            continue
        if repository.casefold() in excluded_repositories:
            continue

        sha = str(_record_value(raw, "sha", default="")).strip().lower()
        if not SHA_RE.fullmatch(sha):
            continue
        if sha in seen_shas:
            continue

        author_login = str(_record_value(raw, "author_login", default="") or "")
        author_name = str(_record_value(raw, "author_name", default="") or "")
        author_email = str(_record_value(raw, "author_email", default="") or "")
        committer_login = str(
            _record_value(raw, "committer_login", default="") or ""
        )
        committer_name = str(_record_value(raw, "committer_name", default="") or "")
        committer_email = str(
            _record_value(raw, "committer_email", default="") or ""
        )

        # A null/unlinked GitHub author is deliberately omitted: identity cannot
        # be proven from a name or email alone.
        if username and author_login.casefold() != username.casefold():
            continue
        if not author_login:
            continue
        if is_bot_identity(author_login, author_name, author_email):
            continue
        if is_bot_identity(committer_login, committer_name, committer_email):
            continue

        message = str(_record_value(raw, "message", default="") or "")
        if should_exclude_message(message, patterns):
            continue
        if bool(_record_value(raw, "is_merge", default=False)) or _parent_count(raw) > 1:
            continue

        try:
            authored_at = _commit_datetime(raw)
        except ValueError:
            continue

        commit_type, title = parse_commit_message(message)
        language = str(_record_value(raw, "language", default="") or "기타")
        url = f"https://github.com/{repository}/commit/{sha}"
        filtered.append(
            Commit(
                sha=sha,
                repository=repository,
                message=message,
                title=title,
                commit_type=commit_type,
                authored_at=authored_at,
                url=url,
                language=language,
                author_login=author_login,
                author_name=author_name,
                author_email=author_email,
                committer_login=committer_login,
                committer_name=committer_name,
                committer_email=committer_email,
            )
        )
        seen_shas.add(sha)

    filtered.sort(key=lambda item: item.authored_at, reverse=True)
    return filtered


def select_commits(commits: Sequence[Any], max_commits: int = 3) -> list[Any]:
    """Return the newest unique commits in descending chronological order."""

    if max_commits <= 0:
        return []
    ordered = sorted(commits, key=_commit_datetime, reverse=True)
    selected: list[Any] = []
    selected_shas: set[str] = set()

    for commit in ordered:
        sha = str(_record_value(commit, "sha", default="")).casefold()
        if not sha or sha in selected_shas:
            continue
        selected.append(commit)
        selected_shas.add(sha)
        if len(selected) == max_commits:
            break
    return selected


def relative_time(
    value: datetime, now: datetime, tz: str | tzinfo = "Asia/Seoul"
) -> str:
    zone = timezone_for(tz)
    local_value = parse_datetime(value).astimezone(zone)
    local_now = parse_datetime(now).astimezone(zone)
    if local_value > local_now:
        local_value = local_now

    delta = local_now - local_value
    date_difference = (local_now.date() - local_value.date()).days
    if date_difference == 0:
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "방금 전"
        if seconds < 3600:
            return f"{seconds // 60}분 전"
        return f"{seconds // 3600}시간 전"
    if date_difference == 1:
        return "어제"
    if date_difference < 7:
        return f"{date_difference}일 전"
    if date_difference < 30:
        return f"{max(1, date_difference // 7)}주 전"
    if date_difference < 365:
        return f"{max(1, date_difference // 30)}개월 전"
    return f"{max(1, date_difference // 365)}년 전"


def _activity_datetimes(values: Iterable[Any]) -> Iterable[datetime]:
    for value in values:
        if isinstance(value, datetime):
            yield value
            continue
        try:
            yield _commit_datetime(value)
        except (TypeError, ValueError):
            continue


def activity_dates(
    commits_or_datetimes: Iterable[Any], tz: str | tzinfo = "Asia/Seoul"
) -> set[date]:
    zone = timezone_for(tz)
    return {
        value.astimezone(zone).date() for value in _activity_datetimes(commits_or_datetimes)
    }


def calculate_streak(
    commits_or_datetimes: Iterable[Any],
    now: datetime,
    tz: str | tzinfo = "Asia/Seoul",
) -> int:
    zone = timezone_for(tz)
    today = parse_datetime(now).astimezone(zone).date()
    active = activity_dates(commits_or_datetimes, zone)
    if today in active:
        cursor = today
    elif today - timedelta(days=1) in active:
        cursor = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor in active:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def calculate_stats(
    commits: Sequence[Commit],
    now: datetime,
    tz: str | tzinfo,
    stats_days: int,
) -> dict[str, int]:
    zone = timezone_for(tz)
    local_now = parse_datetime(now).astimezone(zone)
    first_date = local_now.date() - timedelta(days=stats_days - 1)
    visible = [
        commit
        for commit in commits
        if commit.authored_at.astimezone(zone) <= local_now
    ]
    recent = [
        commit
        for commit in visible
        if first_date <= commit.authored_at.astimezone(zone).date() <= local_now.date()
    ]
    return {
        "stats_days": stats_days,
        "commit_count": len(recent),
        "repository_count": len(
            {commit.repository.casefold() for commit in recent}
        ),
        "streak": calculate_streak(visible, local_now, zone),
    }


def _is_variation_selector(char: str) -> bool:
    codepoint = ord(char)
    return 0xFE00 <= codepoint <= 0xFE0F


def _is_emoji_modifier(char: str) -> bool:
    codepoint = ord(char)
    return 0x1F3FB <= codepoint <= 0x1F3FF


def _graphemes(text: str) -> list[str]:
    """A small stdlib grapheme approximation that keeps combining/ZWJ clusters."""

    clusters: list[str] = []
    current = ""
    join_next = False
    regional_count = 0
    for char in text:
        codepoint = ord(char)
        is_regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        extends = (
            bool(current)
            and (
                join_next
                or char == "\u200d"
                or unicodedata.combining(char) != 0
                or _is_variation_selector(char)
                or _is_emoji_modifier(char)
            )
        )
        if is_regional and current and regional_count == 1:
            extends = True

        if not current or extends:
            current += char
        else:
            clusters.append(current)
            current = char

        if char == "\u200d":
            join_next = True
        else:
            join_next = False
        regional_count = regional_count + 1 if is_regional else 0
        if regional_count >= 2:
            regional_count = 0
    if current:
        clusters.append(current)
    return clusters


def _cluster_width(cluster: str) -> int:
    width = 0
    for char in cluster:
        if char == "\u200d" or unicodedata.combining(char):
            continue
        if _is_variation_selector(char) or _is_emoji_modifier(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    if "\u200d" in cluster:
        return 2
    return max(1, width)


def truncate_text(text: str, max_units: int) -> str:
    """Truncate by display width without cutting Korean or Unicode clusters."""

    if max_units <= 0:
        return ""
    clusters = _graphemes(str(text))
    if sum(_cluster_width(cluster) for cluster in clusters) <= max_units:
        return str(text)
    if max_units == 1:
        return "…"

    result: list[str] = []
    used = 0
    for cluster in clusters:
        width = _cluster_width(cluster)
        if used + width + 1 > max_units:
            break
        result.append(cluster)
        used += width
    return "".join(result).rstrip() + "…"


def xml_escape(value: str) -> str:
    def valid_xml_character(char: str) -> bool:
        codepoint = ord(char)
        return (
            codepoint in {0x09, 0x0A, 0x0D}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        )

    cleaned = "".join(
        char if valid_xml_character(char) else "�" for char in str(value)
    )
    return html.escape(cleaned, quote=True)


class GitHubAPI:
    """Tiny urllib GitHub client with explicit headers and safe failures."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.base_url = "https://api.github.com"

    def get_json(self, path: str) -> tuple[Any, dict[str, str]]:
        url = path if path.startswith("https://") else f"{self.base_url}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "cora1022-recent-public-commits",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                response_headers = {
                    key.casefold(): value for key, value in response.headers.items()
                }
        except urllib.error.HTTPError as exc:
            body = exc.read()
            response_headers = {
                key.casefold(): value for key, value in exc.headers.items()
            }
            try:
                payload = json.loads(body.decode("utf-8"))
                detail = payload.get("message", "") if isinstance(payload, dict) else ""
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = ""
            rate_detail = self._rate_limit_detail(response_headers)
            message = f"GitHub API 요청 실패 ({exc.code}): {detail or exc.reason}"
            if rate_detail:
                message += f" ({rate_detail})"
            raise GitHubAPIError(message, status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"GitHub API 네트워크 연결 실패: {exc.reason}") from exc
        except TimeoutError as exc:
            raise GitHubAPIError("GitHub API 요청 시간이 초과되었습니다.") from exc

        try:
            return json.loads(body.decode("utf-8")), response_headers
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubAPIError("GitHub API가 올바른 JSON을 반환하지 않았습니다.") from exc

    @staticmethod
    def _rate_limit_detail(headers: Mapping[str, str]) -> str:
        details: list[str] = []
        if headers.get("retry-after"):
            details.append(f"retry-after={headers['retry-after']}초")
        if headers.get("x-ratelimit-remaining") is not None:
            details.append(f"remaining={headers['x-ratelimit-remaining']}")
        reset = headers.get("x-ratelimit-reset")
        if reset:
            try:
                reset_time = datetime.fromtimestamp(int(reset), timezone.utc)
                details.append(f"reset={reset_time.isoformat()}")
            except ValueError:
                details.append(f"reset={reset}")
        return ", ".join(details)


def _has_next_page(headers: Mapping[str, str]) -> bool:
    return bool(
        re.search(r'<[^>]+>;\s*rel="next"', headers.get("link", ""), re.IGNORECASE)
    )


def _search_query(
    username: str,
    start_date: date | None = None,
    end_date: date | None = None,
    tz: str | tzinfo = "Asia/Seoul",
) -> str:
    parts = [f"author:{username}", "is:public", "merge:false"]
    if start_date is not None and end_date is not None:
        zone = timezone_for(tz)
        start = datetime.combine(start_date, time.min, zone).astimezone(timezone.utc)
        end = (
            datetime.combine(end_date + timedelta(days=1), time.min, zone)
            - timedelta(seconds=1)
        ).astimezone(timezone.utc)
        start_text = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_text = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(f"author-date:{start_text}..{end_text}")
    return " ".join(parts)


def _fetch_search_page(
    api: GitHubAPI, query: str, page: int
) -> tuple[dict[str, Any], dict[str, str]]:
    parameters = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "author-date",
            "order": "desc",
            "per_page": 100,
            "page": page,
        }
    )
    data, headers = api.get_json(f"/search/commits?{parameters}")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise GitHubAPIError("GitHub Commit Search 응답 형식이 올바르지 않습니다.")
    return data, headers


def _fetch_complete_search(
    api: GitHubAPI, query: str
) -> list[dict[str, Any]]:
    first, headers = _fetch_search_page(api, query, 1)
    total_count = int(first.get("total_count", 0))
    if bool(first.get("incomplete_results")) or total_count > 1000:
        raise SearchNeedsSplit(
            "GitHub Commit Search 결과가 불완전하거나 1,000개를 초과했습니다."
        )

    items = list(first["items"])
    page = 1
    while _has_next_page(headers):
        page += 1
        if page > 10:
            raise SearchNeedsSplit("GitHub Commit Search 1,000개 한도에 도달했습니다.")
        result, headers = _fetch_search_page(api, query, page)
        if bool(result.get("incomplete_results")):
            raise SearchNeedsSplit("GitHub Commit Search가 불완전한 결과를 반환했습니다.")
        items.extend(result["items"])

    if len(items) < total_count:
        raise SearchNeedsSplit(
            "GitHub Commit Search 페이지네이션이 전체 결과를 반환하지 않았습니다."
        )
    return items


def _search_interval(
    api: GitHubAPI,
    username: str,
    start_date: date,
    end_date: date,
    tz: str | tzinfo,
) -> list[dict[str, Any]]:
    query = _search_query(username, start_date, end_date, tz)
    try:
        return _fetch_complete_search(api, query)
    except SearchNeedsSplit as exc:
        if start_date >= end_date:
            raise GitHubAPIError(
                f"{start_date.isoformat()} 하루의 공개 커밋 검색을 완전하게 "
                "가져올 수 없어 기존 카드를 보존합니다."
            ) from exc
        days = (end_date - start_date).days
        midpoint = start_date + timedelta(days=days // 2)
        return _search_interval(
            api, username, start_date, midpoint, tz
        ) + _search_interval(
            api, username, midpoint + timedelta(days=1), end_date, tz
        )


def _identity_fields(value: Any, prefix: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {
            f"{prefix}_login": "",
            f"{prefix}_name": "",
            f"{prefix}_email": "",
        }
    return {
        f"{prefix}_login": str(value.get("login") or ""),
        f"{prefix}_name": str(value.get("name") or ""),
        f"{prefix}_email": str(value.get("email") or ""),
    }


def _search_item_to_raw(item: Mapping[str, Any]) -> dict[str, Any]:
    repository = item.get("repository")
    commit_data = item.get("commit")
    if not isinstance(repository, Mapping) or not isinstance(commit_data, Mapping):
        return {}

    author_data = commit_data.get("author")
    committer_data = commit_data.get("committer")
    if not isinstance(author_data, Mapping):
        author_data = {}
    if not isinstance(committer_data, Mapping):
        committer_data = {}

    linked_author = _identity_fields(item.get("author"), "author")
    linked_committer = _identity_fields(item.get("committer"), "committer")
    linked_author["author_name"] = str(author_data.get("name") or "")
    linked_author["author_email"] = str(author_data.get("email") or "")
    linked_committer["committer_name"] = str(committer_data.get("name") or "")
    linked_committer["committer_email"] = str(committer_data.get("email") or "")

    return {
        "sha": item.get("sha", ""),
        "repository": repository.get("full_name", ""),
        "message": commit_data.get("message", ""),
        "authored_at": author_data.get("date"),
        "language": repository.get("language") or "기타",
        # Require an explicit false value; missing privacy metadata is not trusted.
        "private": repository.get("private") is not False,
        "parents": item.get("parents") or [],
        **linked_author,
        **linked_committer,
    }


def collect_public_commits(
    api: GitHubAPI, config: Mapping[str, Any], now: datetime
) -> list[Commit]:
    """Collect complete KST windows for statistics, streak, and diversity."""

    username = str(config["username"])
    zone = timezone_for(str(config["timezone"]))
    local_now = parse_datetime(now).astimezone(zone)
    today = local_now.date()
    stats_days = int(config["stats_days"])
    history_floor = date(2008, 1, 1)

    def normalize_visible(items: Sequence[Mapping[str, Any]]) -> list[Commit]:
        normalized = normalize_and_filter_commits(
            [_search_item_to_raw(item) for item in items], config
        )
        return [
            commit
            for commit in normalized
            if commit.authored_at.astimezone(zone) <= local_now
        ]

    window_days = max(35, stats_days)
    current_start = today - timedelta(days=window_days - 1)
    current_end = today
    search_items = _search_interval(
        api, username, current_start, current_end, zone
    )
    commits = normalize_visible(search_items)

    # If activity is unbroken through the oldest queried date, expand backward
    # until a real inactive day is observed. This avoids silently capping streaks.
    while True:
        active = activity_dates(commits, zone)
        anchor = today if today in active else today - timedelta(days=1)
        if anchor not in active:
            break
        streak = calculate_streak(commits, now, zone)
        covered_days = (anchor - current_start).days + 1
        if streak < covered_days:
            break

        if current_start <= history_floor:
            break
        previous_end = current_start - timedelta(days=1)
        window_days *= 2
        previous_start = max(
            history_floor, previous_end - timedelta(days=window_days - 1)
        )
        search_items.extend(
            _search_interval(api, username, previous_start, previous_end, zone)
        )
        current_start = previous_start
        commits = normalize_visible(search_items)

    # Search older, non-overlapping ranges only if the first window cannot fill
    # every card slot. Card order itself is always strictly chronological.
    max_commits = int(config["max_commits"])
    while current_start > history_floor and len(commits) < max_commits:
        previous_end = current_start - timedelta(days=1)
        window_days *= 2
        previous_start = max(
            history_floor, previous_end - timedelta(days=window_days - 1)
        )
        search_items.extend(
            _search_interval(api, username, previous_start, previous_end, zone)
        )
        current_start = previous_start
        commits = normalize_visible(search_items)
    return commits


def select_and_enrich_repositories(
    api: GitHubAPI, commits: Sequence[Commit], max_commits: int
) -> list[Commit]:
    """Select cards, recheck visibility, and add each primary language.

    Commit Search intentionally supplies only part of the repository object.
    Only repositories selected for display are requested. If one became private
    or disappeared, selection is repeated so an eligible fallback can fill it.
    """

    cache: dict[str, str | None] = {}
    while True:
        candidates = [
            commit
            for commit in commits
            if cache.get(commit.repository.casefold(), "unverified") is not None
        ]
        selected = select_commits(candidates, max_commits)
        found_invalid = False
        for commit in selected:
            key = commit.repository.casefold()
            if key in cache:
                continue
            owner, name = commit.repository.split("/", 1)
            path = "/repos/{}/{}".format(
                urllib.parse.quote(owner, safe=""),
                urllib.parse.quote(name, safe=""),
            )
            try:
                data, _ = api.get_json(path)
            except GitHubAPIError as exc:
                # A repository can disappear or become private between search
                # indexing and this verification. Never render its cached text.
                if exc.status == 404:
                    cache[key] = None
                    found_invalid = True
                    continue
                raise
            if not isinstance(data, Mapping) or data.get("private") is not False:
                cache[key] = None
                found_invalid = True
                continue
            full_name = str(data.get("full_name") or "")
            if full_name.casefold() != key:
                raise GitHubAPIError(
                    "저장소 공개 여부 검증 응답의 이름이 일치하지 않습니다."
                )
            cache[key] = str(data.get("language") or "기타")

        if found_invalid:
            continue
        return [
            replace(commit, language=str(cache[commit.repository.casefold()]))
            for commit in selected
        ]


def _svg_root(
    label_id: str, description_id: str, height: int, width: int = 720
) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="{label_id} {description_id}">'
    )


def render_header_svg(_stats: Mapping[str, int]) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{_svg_root("title", "desc", 84)}
  <title id="title">요즘 커밋한거</title>
  <desc id="desc">검정 깃허브 고양이 낙서와 보라색 밑줄로 꾸민 최근 커밋 섹션 제목</desc>
  <rect width="720" height="84" fill="#FFFFFF"/>

  <g font-family="Pretendard, Noto Sans KR, Apple SD Gothic Neo, Segoe UI, sans-serif" fill="#24292F">
    <g transform="translate(-2 3) scale(1.25) rotate(-4 20 20)">
      <path d="M8 16 L6 7 L14 11 C19 9 25 9 30 12 L38 8 L35 18 C38 23 36 31 30 35 C24 39 15 38 9 34 C3 30 2 21 8 16Z" fill="#24292F" stroke="#24292F" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="15" cy="23" r="1.8" fill="#FFFFFF"/>
      <circle cx="29" cy="23" r="1.8" fill="#FFFFFF"/>
      <path d="M20 28 Q22 30 24 28 M22 30 V32" fill="none" stroke="#FFFFFF" stroke-width="1.7" stroke-linecap="round"/>
      <path d="M11 28 L3 26 M11 31 L3 33 M33 28 L41 26 M33 31 L41 34" fill="none" stroke="#24292F" stroke-width="1.7" stroke-linecap="round"/>
    </g>
    <!-- 53:41 README floats make this SVG render narrower than the featured header. -->
    <text x="61" y="60" font-size="36" font-weight="800">요즘 커밋한거</text>
    <path d="M63 74 C109 70 158 77 210 72 C229 71 244 74 260 72" fill="none" stroke="#8B6CF6" stroke-width="3" stroke-linecap="round"/>
  </g>
</svg>
"""


def _commit_type_icon_svg(commit_type: str) -> str:
    """Return a small fixed doodle icon for the commit category."""

    icons = {
        "FEAT": '<path d="M35 61 H55 M45 51 V71" fill="none" stroke="#24292F" stroke-width="4" stroke-linecap="round"/>',
        "FIX": '<path d="M34 62 L41 69 L56 52" fill="none" stroke="#24292F" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>',
        "REFACTOR": '<path d="M37 52 V70 M37 55 C43 55 48 57 52 62 M52 57 V68 M33 51 A4 4 0 1 0 41 51 A4 4 0 1 0 33 51 M48 72 A4 4 0 1 0 56 72 A4 4 0 1 0 48 72" fill="none" stroke="#24292F" stroke-width="2.6" stroke-linecap="round"/>',
        "DOCS": '<path d="M37 50 H50 L56 56 V72 H37 Z M50 50 V57 H56 M41 62 H51 M41 67 H49" fill="none" stroke="#24292F" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>',
        "TEST": '<path d="M39 49 H51 M42 49 V57 L36 70 Q35 73 39 73 H55 Q59 73 57 69 L51 57 V49 M40 65 C44 62 50 69 55 64" fill="none" stroke="#24292F" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
        "PERF": '<path d="M49 48 L36 63 H45 L41 75 L56 58 H48 Z" fill="#24292F" stroke="#24292F" stroke-width="1.5" stroke-linejoin="round"/>',
        "BUILD": '<path d="M36 53 L45 48 L54 53 V63 L45 68 L36 63 Z M45 48 V58 M36 53 L45 58 L54 53 M45 58 V68" fill="none" stroke="#24292F" stroke-width="2.3" stroke-linejoin="round"/>',
        "CI": '<path d="M37 56 C39 49 50 48 54 54 M53 50 L54 55 L49 56 M54 66 C51 73 40 74 36 68 M37 72 L36 67 L41 66" fill="none" stroke="#24292F" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>',
        "STYLE": '<path d="M53 49 C58 55 52 60 48 64 C44 68 42 73 35 72 C40 68 38 64 42 61 L51 50 Z" fill="none" stroke="#24292F" stroke-width="2.5" stroke-linejoin="round"/>',
        "REVERT": '<path d="M38 56 L33 61 L38 66 M34 61 H47 C55 61 57 70 52 74" fill="none" stroke="#24292F" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>',
        "CHORE": '<path d="M45 49 V73 M33 61 H57 M37 53 L53 69 M53 53 L37 69" fill="none" stroke="#24292F" stroke-width="2.5" stroke-linecap="round"/>',
        "COMMIT": '<path d="M36 55 L29 62 L36 69 M54 55 L61 62 L54 69 M49 51 L41 73" fill="none" stroke="#24292F" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>',
    }
    return icons.get(commit_type, icons["COMMIT"])


def render_commit_svg(commit: Commit, now: datetime, tz: str | tzinfo) -> str:
    commit_type = commit.commit_type if commit.commit_type in TYPE_COLORS else "COMMIT"
    color = TYPE_COLORS[commit_type]
    repository_name = commit.repository.split("/", 1)[-1]
    repository_accessible = xml_escape(repository_name)
    repository = xml_escape(truncate_text(repository_name, 30))
    title = xml_escape(truncate_text(commit.title, 30))
    language = xml_escape(truncate_text(commit.language, 12))
    short_sha = xml_escape(commit.sha[:7])
    when = xml_escape(relative_time(commit.authored_at, now, tz))
    description = xml_escape(
        f"{commit_type}, {repository_name}, {commit.title}, {commit.sha[:7]}, "
        f"{commit.language}, {relative_time(commit.authored_at, now, tz)}"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{_svg_root("title", "desc", 148, 420)}
  <title id="title">{repository_accessible} 최근 공개 커밋</title>
  <desc id="desc">{description}</desc>
  <path d="M13 7 C76 3 145 10 211 6 C280 2 350 11 407 5 Q416 6 413 17 C416 54 411 94 415 133 Q413 143 403 142 C333 146 267 139 199 143 C130 146 64 140 13 144 Q5 142 8 133 C5 94 10 54 6 17 Q7 9 13 7Z" fill="#FFFFFF" stroke="#24292F" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M13 14 C10 45 14 95 12 134" fill="none" stroke="{color}" stroke-width="3" stroke-linecap="round" opacity=".85"/>

  <g font-family="Pretendard, Noto Sans KR, Apple SD Gothic Neo, Segoe UI, sans-serif" fill="#24292F">
    <circle cx="45" cy="62" r="27" fill="{color}"/>
    <circle cx="43" cy="59" r="23" fill="#FFFFFF" opacity=".16"/>
    {_commit_type_icon_svg(commit_type)}
    <text x="45" y="106" text-anchor="middle" font-size="11" font-weight="850" fill="{color}" letter-spacing=".4">{commit_type}</text>

    <g transform="translate(377 16) rotate(-5 10 10)">
      <path d="M2 18 C8 12 12 8 18 3 M9 3 H18 V12" fill="none" stroke="{color}" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M4 22 C9 19 14 22 20 19" fill="none" stroke="#24292F" stroke-width="1.2" stroke-linecap="round"/>
    </g>

    <text x="87" y="44" font-size="17" font-weight="780">{title}</text>
    <path d="M88 53 C132 49 176 55 222 51 C246 49 267 53 288 51" fill="none" stroke="{color}" stroke-width="2.2" stroke-linecap="round" opacity=".9"/>
    <text x="87" y="78" font-size="13" font-weight="680" fill="#24292F">{repository}</text>
    <circle cx="91" cy="105" r="3" fill="{color}"/>
    <text x="101" y="110" font-size="12.5" font-weight="620" fill="#66707A">{short_sha} · {language}</text>
    <text x="393" y="110" text-anchor="end" font-size="12.5" font-weight="650" fill="#66707A">{when}</text>
    <path d="M88 126 C164 122 243 129 319 125" fill="none" stroke="#D8DEE4" stroke-width="1.3" stroke-linecap="round" stroke-dasharray="5 6"/>
    <path d="M340 135 C356 130 369 139 384 133 C394 129 401 131 407 127" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" opacity=".55"/>
  </g>
</svg>
"""


def render_empty_svg() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
{_svg_root("title", "desc", 116)}
  <title id="title">최근 공개 커밋 없음</title>
  <desc id="desc">최근 표시할 공개 커밋이 없습니다. 새로운 작업이 올라오면 이곳에 자동으로 표시됩니다.</desc>
  <path d="M15 7 C133 5 238 10 355 7 C480 5 587 10 703 7 Q712 8 711 19 L713 99 Q711 109 699 108 C566 112 449 106 330 110 C210 112 109 107 17 110 Q8 109 10 98 L8 21 Q9 10 15 7Z" fill="#FFFFFF" stroke="#24292F" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
  <g transform="translate(34 31)">
    <circle cx="21" cy="21" r="15" fill="#FFF3B8" stroke="#24292F" stroke-width="1.7"/>
    <path d="M21 0 V5 M21 37 V42 M0 21 H5 M37 21 H42 M6 6 L10 10 M32 32 L36 36 M36 6 L32 10 M10 32 L6 36" fill="none" stroke="#FFC94A" stroke-width="2.2" stroke-linecap="round"/>
    <path d="M14 23 C18 27 24 27 29 22" fill="none" stroke="#24292F" stroke-width="1.6" stroke-linecap="round"/>
    <circle cx="16" cy="17" r="1.5" fill="#24292F"/><circle cx="27" cy="17" r="1.5" fill="#24292F"/>
  </g>
  <g font-family="Pretendard, Noto Sans KR, Apple SD Gothic Neo, Segoe UI, sans-serif">
    <text x="94" y="49" font-size="19" font-weight="750" fill="#24292F">최근 표시할 공개 커밋이 없습니다.</text>
    <text x="94" y="76" font-size="16.5" font-weight="600" fill="#66707A">새로운 작업이 올라오면 이곳에 자동으로 표시됩니다.</text>
  </g>
  <path d="M578 89 C607 75 632 95 655 78 C670 68 685 73 695 62" fill="none" stroke="#8B6CF6" stroke-width="2" stroke-linecap="round" stroke-dasharray="7 7" opacity=".55"/>
</svg>
"""


def build_readme_block(
    commits: Sequence[Commit], newline: str = "\n"
) -> str:
    lines = [
        START_MARKER,
        "",
        '<div align="center">',
        '  <img src="./assets/recent-commits/header.svg" width="100%" alt="요즘 커밋한거">',
        "  <br>",
    ]
    if commits:
        for index, commit in enumerate(commits, start=1):
            url = html.escape(commit.url, quote=True)
            lines.append(
                f'  <a href="{url}"><img '
                f'src="./assets/recent-commits/commit-{index}.svg" width="96%" '
                f'alt="최근 공개 커밋 {index}"></a>'
            )
            if index < len(commits):
                lines.append("  <br>")
    else:
        lines.append(
            '  <img src="./assets/recent-commits/commit-1.svg" width="96%" '
            'alt="최근 공개 커밋 없음">'
        )
    lines.extend(["</div>", "", END_MARKER])
    return newline.join(lines)


def _slot_marker(index: int, edge: str) -> str:
    return f"<!-- RECENT_COMMIT_{index}_{edge} -->"


def _build_readme_slot(
    commit: Commit | None,
    index: int,
    newline: str,
    *,
    empty_state: bool = False,
) -> str:
    lines = [_slot_marker(index, "START")]
    if commit is not None:
        url = html.escape(commit.url, quote=True)
        lines.append(
            f'  <a href="{url}"><img '
            f'src="./assets/recent-commits/commit-{index}.svg" width="41%" '
            f'align="right" alt="최근 공개 커밋 {index}"></a>'
        )
    elif empty_state:
        lines.append(
            '  <img src="./assets/recent-commits/commit-1.svg" width="41%" '
            'align="right" alt="최근 공개 커밋 없음">'
        )
    lines.append(_slot_marker(index, "END"))
    return newline.join(lines)


def has_readme_slots(original: str) -> bool:
    """Return whether README uses the optional borderless float layout."""

    return any(
        _slot_marker(index, edge) in original
        for index in range(1, SLOT_COUNT + 1)
        for edge in ("START", "END")
    )


def replace_readme_slots(
    original: str, commits: Sequence[Commit], newline: str = "\n"
) -> str:
    """Update float-layout commit slots without rebuilding personal project markup."""

    if original.count(START_MARKER) != 1 or original.count(END_MARKER) != 1:
        raise GeneratorError("README 최근 커밋 마커는 시작/끝이 각각 하나여야 합니다.")

    section_start = original.index(START_MARKER)
    section_end = original.index(END_MARKER, section_start)
    previous_end = section_start + len(START_MARKER)
    replacements: list[tuple[int, int, str]] = []
    for index in range(1, SLOT_COUNT + 1):
        start_marker = _slot_marker(index, "START")
        end_marker = _slot_marker(index, "END")
        if original.count(start_marker) != 1 or original.count(end_marker) != 1:
            raise GeneratorError(
                f"README 최근 커밋 {index}번 슬롯 마커는 시작/끝이 각각 하나여야 합니다."
            )
        start = original.index(start_marker)
        end = original.index(end_marker, start) + len(end_marker)
        if start < previous_end or end > section_end:
            raise GeneratorError(
                "README 최근 커밋 슬롯은 바깥 마커 안에서 1~3번 순서로 배치해야 합니다."
            )
        previous_end = end
        commit = commits[index - 1] if index <= len(commits) else None
        replacement = _build_readme_slot(
            commit,
            index,
            newline,
            empty_state=not commits and index == 1,
        )
        replacements.append((start, end, replacement))

    result = original
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]
    return result


def replace_readme_section(original: str, block: str) -> str:
    """Replace exactly the generated range while preserving all outside bytes."""

    start_count = original.count(START_MARKER)
    end_count = original.count(END_MARKER)
    if start_count != end_count:
        raise GeneratorError("README 최근 커밋 마커의 시작/끝 개수가 다릅니다.")
    if start_count > 1:
        raise GeneratorError("README 최근 커밋 마커가 중복되어 있습니다.")
    if start_count == 1:
        start = original.index(START_MARKER)
        end = original.index(END_MARKER, start) + len(END_MARKER)
        return original[:start] + block + original[end:]

    heading = re.search(
        r'(?m)^(?:##\s+관심 (?:분야|있는거)\s*|'
        r'<img src="\./assets/section-headers/interests\.svg"[^>]*>|'
        r'<div><img src="\./assets/section-headers/interests\.svg"[^>]*></div>)\r?$',
        original,
    )
    if heading is None:
        heading = re.search(r"(?m)^##\s+대표 프로젝트\s*\r?$", original)

    newline = "\r\n" if "\r\n" in original else "\n"
    if heading is not None:
        return original[: heading.start()] + block + newline * 2 + original[heading.start() :]

    if not original:
        return block + newline
    separator = "" if original.endswith(newline * 2) else (
        newline if original.endswith(newline) else newline * 2
    )
    return original + separator + block + newline


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _atomic_write_text(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return True


def write_outputs(
    output_dir: Path,
    header_svg: str,
    card_svgs: Sequence[str],
    readme_path: Path | None,
    readme_content: str | None,
) -> list[Path]:
    """Validate all SVG XML before atomically replacing any generated file."""

    generated = {"header.svg": header_svg}
    for index, content in enumerate(card_svgs, start=1):
        generated[f"commit-{index}.svg"] = content
    for name, content in generated.items():
        try:
            ET.fromstring(content)
        except ET.ParseError as exc:
            raise GeneratorError(f"{name} SVG XML 검증 실패: {exc}") from exc

    changed: list[Path] = []
    for name, content in generated.items():
        path = output_dir / name
        if _atomic_write_text(path, content):
            changed.append(path)

    expected = set(generated)
    for stale in output_dir.glob("commit-*.svg"):
        if stale.name not in expected:
            stale.unlink()
            changed.append(stale)

    if readme_path is not None and readme_content is not None:
        if _atomic_write_text(readme_path, readme_content):
            changed.append(readme_path)
    return changed


def _infer_username() -> str:
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip()
    if owner:
        return owner
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in repository:
        return repository.split("/", 1)[0]
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    remote = result.stdout.strip()
    match = re.search(
        r"(?:github\.com[/:])(?P<owner>[A-Za-z0-9_.-]+)/[A-Za-z0-9_.-]+(?:\.git)?$",
        remote,
        re.IGNORECASE,
    )
    return match.group("owner") if match else ""


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GeneratorError(f"설정 파일을 찾을 수 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GeneratorError(f"설정 JSON 문법 오류: {exc}") from exc
    if not isinstance(data, dict):
        raise GeneratorError("설정 파일의 최상위 값은 JSON 객체여야 합니다.")

    username = str(data.get("username") or _infer_username()).strip()
    if not username:
        raise GeneratorError(
            "GitHub 사용자명을 추론할 수 없습니다. 설정의 username을 지정하세요."
        )
    data["username"] = username
    data.setdefault("max_commits", 3)
    data.setdefault("stats_days", 7)
    data.setdefault("timezone", "Asia/Seoul")
    data.setdefault("exclude_repositories", [f"{username}/{username}"])
    data.setdefault("exclude_message_patterns", list(DEFAULT_EXCLUDE_PATTERNS))

    try:
        max_commits = int(data["max_commits"])
        stats_days = int(data["stats_days"])
    except (TypeError, ValueError) as exc:
        raise GeneratorError("max_commits와 stats_days는 정수여야 합니다.") from exc
    if not 1 <= max_commits <= 3:
        raise GeneratorError("max_commits는 1 이상 3 이하여야 합니다.")
    if not 1 <= stats_days <= 31:
        raise GeneratorError("stats_days는 1 이상 31 이하여야 합니다.")
    data["max_commits"] = max_commits
    data["stats_days"] = stats_days
    timezone_for(str(data["timezone"]))

    if not isinstance(data["exclude_repositories"], list):
        raise GeneratorError("exclude_repositories는 문자열 배열이어야 합니다.")
    if not isinstance(data["exclude_message_patterns"], list):
        raise GeneratorError("exclude_message_patterns는 정규식 문자열 배열이어야 합니다.")
    for pattern in data["exclude_message_patterns"]:
        try:
            re.compile(str(pattern), re.IGNORECASE)
        except re.error as exc:
            raise GeneratorError(f"제외 메시지 정규식 오류 {pattern!r}: {exc}") from exc
    return data


def fixture_commits(config: Mapping[str, Any], now: datetime) -> list[Commit]:
    username = str(config["username"])
    raw = [
        {
            "sha": "a92f71c" + "0" * 33,
            "repository": f"{username}/smishing-website-project",
            "message": "feat: URL 분석 결과에 위험 근거 요약 기능 추가",
            "authored_at": now - timedelta(hours=2),
            "language": "Python",
            "private": False,
            "author_login": username,
            "author_name": username,
            "author_email": f"{username}@users.noreply.github.com",
        },
        {
            "sha": "f31c8ad" + "1" * 33,
            "repository": f"{username}/gemini-raspberry-pi-robot",
            "message": "fix: 카메라 스트림 연결 해제 시 자동 재연결 처리",
            "authored_at": now - timedelta(days=1),
            "language": "Python",
            "private": False,
            "author_login": username,
            "author_name": username,
            "author_email": f"{username}@users.noreply.github.com",
        },
        {
            "sha": "7be42d1" + "2" * 33,
            "repository": f"{username}/Keyboard-Warrior",
            "message": "refactor: 실시간 매칭 서비스와 전투 로직 분리",
            "authored_at": now - timedelta(days=3),
            "language": "TypeScript",
            "private": False,
            "author_login": username,
            "author_name": username,
            "author_email": f"{username}@users.noreply.github.com",
        },
        {
            "sha": "d18a50e" + "3" * 33,
            "repository": f"{username}/portfolio",
            "message": "docs: GitHub Actions 자동 실행 과정 정리",
            "authored_at": now - timedelta(days=4),
            "language": "JavaScript",
            "private": False,
            "author_login": username,
            "author_name": username,
            "author_email": f"{username}@users.noreply.github.com",
        },
        {
            "sha": "3c8b4f2" + "4" * 33,
            "repository": f"{username}/url-safety-checker",
            "message": "test: URL 판별 경계값 테스트 추가",
            "authored_at": now - timedelta(days=5),
            "language": "Python",
            "private": False,
            "author_login": username,
            "author_name": username,
            "author_email": f"{username}@users.noreply.github.com",
        },
        {
            "sha": "5f279ab" + "5" * 33,
            "repository": f"{username}/study-notes",
            "message": "chore: 프로젝트 실행 예제 정리",
            "authored_at": now - timedelta(days=6),
            "language": "Markdown",
            "private": False,
            "author_login": username,
            "author_name": username,
            "author_email": f"{username}@users.noreply.github.com",
        },
    ]
    return normalize_and_filter_commits(raw, config)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="최근 공개 커밋 SVG 카드와 프로필 README를 갱신합니다."
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="설정 JSON 경로"
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="API 대신 예제 데이터로 preview/recent-commits에 생성",
    )
    parser.add_argument("--output-dir", type=Path, help="SVG 출력 디렉터리")
    parser.add_argument("--readme", type=Path, default=DEFAULT_README_PATH)
    parser.add_argument(
        "--update-readme",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="README 마커 영역 갱신 여부(기본: 실제 모드만 갱신)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config.resolve())
        zone = timezone_for(str(config["timezone"]))
        now = datetime.now(timezone.utc).astimezone(zone)
        update_readme = (not args.fixture) if args.update_readme is None else args.update_readme
        output_dir = (
            args.output_dir.resolve()
            if args.output_dir
            else (DEFAULT_PREVIEW_DIR if args.fixture else DEFAULT_OUTPUT_DIR)
        )

        if args.fixture:
            all_commits = fixture_commits(config, now)
        else:
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not token:
                print(
                    "주의: GITHUB_TOKEN/GH_TOKEN 없이 공개 API의 낮은 rate limit으로 실행합니다.",
                    file=sys.stderr,
                )
            api = GitHubAPI(token)
            all_commits = collect_public_commits(api, config, now)

        selected = (
            select_commits(all_commits, int(config["max_commits"]))
            if args.fixture
            else select_and_enrich_repositories(
                api, all_commits, int(config["max_commits"])
            )
        )
        stats = calculate_stats(
            all_commits, now, zone, int(config["stats_days"])
        )
        header_svg = render_header_svg(stats)
        card_svgs = (
            [render_commit_svg(commit, now, zone) for commit in selected]
            if selected
            else [render_empty_svg()]
        )

        readme_path: Path | None = None
        readme_content: str | None = None
        if update_readme:
            readme_path = args.readme.resolve()
            try:
                original_bytes = readme_path.read_bytes()
                original = original_bytes.decode("utf-8")
            except FileNotFoundError as exc:
                raise GeneratorError(f"README를 찾을 수 없습니다: {readme_path}") from exc
            except UnicodeDecodeError as exc:
                raise GeneratorError("README는 UTF-8이어야 합니다.") from exc
            newline = _detect_newline(original)
            if has_readme_slots(original):
                readme_content = replace_readme_slots(original, selected, newline)
            else:
                block = build_readme_block(selected, newline)
                readme_content = replace_readme_section(original, block)

        changed = write_outputs(
            output_dir, header_svg, card_svgs, readme_path, readme_content
        )
        mode = "fixture preview" if args.fixture else "GitHub API"
        print(
            f"{mode}: {len(selected)}개 카드, 최근 {config['stats_days']}일 "
            f"{stats['commit_count']} commits / {stats['repository_count']} repos / "
            f"{stats['streak']} day streak"
        )
        if changed:
            print(f"변경된 파일: {len(changed)}개")
        else:
            print("내용 변경 없음")
        return 0
    except (GeneratorError, OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
