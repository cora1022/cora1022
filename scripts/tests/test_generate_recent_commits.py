"""Unit tests for the recent public commit card generator.

The suite intentionally uses only in-memory fixtures.  It must never contact the
GitHub API, so it is safe to run both locally and in GitHub Actions.
"""

from __future__ import annotations

import enum
import io
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.generate_recent_commits import (
    Commit,
    GeneratorError,
    GitHubAPIError,
    build_readme_block,
    calculate_stats,
    calculate_streak,
    collect_public_commits,
    has_readme_slots,
    is_bot_identity,
    main,
    normalize_and_filter_commits,
    parse_commit_message,
    relative_time,
    render_commit_svg,
    render_empty_svg,
    render_header_svg,
    replace_readme_section,
    replace_readme_slots,
    select_commits,
    should_exclude_message,
    truncate_text,
    xml_escape,
    _commit_type_icon_svg,
)


try:
    KST = ZoneInfo("Asia/Seoul")
except ZoneInfoNotFoundError:
    # Windows Python does not always ship the IANA database.  The generator
    # deliberately stays stdlib-only and uses the same fixed-offset fallback.
    KST = timezone(timedelta(hours=9), name="Asia/Seoul")
UTC = timezone.utc


def value(record: Any, *names: str) -> Any:
    """Read a field from either a mapping or a dataclass-like result."""

    for name in names:
        if isinstance(record, dict) and name in record:
            result = record[name]
        elif hasattr(record, name):
            result = getattr(record, name)
        else:
            continue

        if name in {"repository", "repo", "repository_full_name"}:
            if isinstance(result, dict):
                return result.get("full_name") or result.get("name")
            if hasattr(result, "full_name"):
                return result.full_name
        return result

    raise AssertionError(f"None of {names!r} exists on {record!r}")


def parsed_type_name(parsed_type: Any) -> str:
    """Normalize string and Enum parser results without constraining internals."""

    if isinstance(parsed_type, enum.Enum):
        parsed_type = parsed_type.value
    return str(parsed_type).upper()


def raw_commit(
    sha: str,
    repository: str,
    message: str,
    committed_at: datetime,
    *,
    author_login: str = "cora1022",
    author_name: str = "Hwang Young-yeon",
    author_email: str = "cora1022@users.noreply.github.com",
    language: str = "Python",
    private: bool = False,
) -> dict[str, Any]:
    """Return the deliberately small, API-independent input record contract."""

    return {
        "sha": sha,
        "repository": repository,
        "message": message,
        "committed_at": committed_at,
        "author_login": author_login,
        "author_name": author_name,
        "author_email": author_email,
        "language": language,
        "private": private,
        "url": f"https://github.com/{repository}/commit/{sha}",
    }


def base_config() -> dict[str, Any]:
    return {
        "username": "cora1022",
        "max_commits": 3,
        "stats_days": 7,
        "timezone": "Asia/Seoul",
        "exclude_repositories": ["cora1022/cora1022"],
        "exclude_message_patterns": [
            r"^Merge pull request",
            r"^Update README\.md$",
            r"update recent commits",
            r"update recent commit cards",
        ],
    }


class ParseCommitMessageTests(unittest.TestCase):
    def test_parses_every_supported_conventional_commit_type(self) -> None:
        supported_types = (
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
        for expected_type in supported_types:
            with self.subTest(commit_type=expected_type):
                commit_type, title = parse_commit_message(
                    f"{expected_type}: 공개 커밋 카드 개선"
                )
                self.assertEqual(parsed_type_name(commit_type), expected_type.upper())
                self.assertEqual(title, "공개 커밋 카드 개선")

    def test_parses_scope_but_omits_it_from_type_and_title(self) -> None:
        commit_type, title = parse_commit_message("fix(api): 응답 오류 수정")

        self.assertEqual(parsed_type_name(commit_type), "FIX")
        self.assertEqual(title, "응답 오류 수정")

    def test_unrecognized_message_is_a_plain_commit(self) -> None:
        original = "첫 번째 공개 버전"
        commit_type, title = parse_commit_message(original)

        self.assertEqual(parsed_type_name(commit_type), "COMMIT")
        self.assertEqual(title, original)


class FilteringTests(unittest.TestCase):
    def test_message_patterns_are_case_insensitive_and_regex_aware(self) -> None:
        patterns = base_config()["exclude_message_patterns"]

        self.assertTrue(should_exclude_message("merge PULL request #19", patterns))
        self.assertTrue(should_exclude_message("UPDATE readme.md", patterns))
        self.assertTrue(
            should_exclude_message("chore: Update Recent Commit Cards", patterns)
        )
        self.assertFalse(
            should_exclude_message("docs: README 사용 방법을 자세히 설명", patterns)
        )

    def test_bot_identity_uses_login_name_and_email(self) -> None:
        bot_cases = (
            ("dependabot[bot]", "dependabot", None),
            ("github-actions[bot]", None, None),
            (None, "GitHub Actions Bot", None),
            (None, None, "41898282+github-actions[bot]@users.noreply.github.com"),
        )
        for identity in bot_cases:
            with self.subTest(identity=identity):
                self.assertTrue(is_bot_identity(*identity))

        self.assertFalse(
            is_bot_identity(
                "cora1022", "Hwang Young-yeon", "cora1022@users.noreply.github.com"
            )
        )
        self.assertFalse(
            is_bot_identity("robotics-dev", "Robotics Developer", "dev@example.com")
        )

    def test_normalization_filters_messages_bots_other_authors_and_private_repos(self) -> None:
        now = datetime(2026, 8, 21, 6, tzinfo=UTC)
        records = [
            raw_commit("1" * 40, "cora1022/project-a", "feat: 정상 작업", now),
            raw_commit(
                "2" * 40,
                "cora1022/project-a",
                "chore: update recent commits",
                now - timedelta(minutes=1),
            ),
            raw_commit(
                "3" * 40,
                "cora1022/project-b",
                "fix: 봇 작업",
                now - timedelta(minutes=2),
                author_login="dependabot[bot]",
                author_name="dependabot[bot]",
                author_email="49699333+dependabot[bot]@users.noreply.github.com",
            ),
            raw_commit(
                "4" * 40,
                "cora1022/project-c",
                "docs: 다른 작성자 작업",
                now - timedelta(minutes=3),
                author_login="someone-else",
                author_name="Someone Else",
                author_email="someone@example.com",
            ),
            raw_commit(
                "5" * 40,
                "cora1022/private-project",
                "feat: 비공개 작업",
                now - timedelta(minutes=4),
                private=True,
            ),
            raw_commit(
                "6" * 40,
                "cora1022/cora1022",
                "docs: 프로필 저장소 작업",
                now - timedelta(minutes=5),
            ),
        ]

        result = normalize_and_filter_commits(records, base_config())

        self.assertEqual([value(item, "sha") for item in result], ["1" * 40])

    def test_normalization_deduplicates_sha_case_insensitively(self) -> None:
        now = datetime(2026, 8, 21, 6, tzinfo=UTC)
        sha = "a92f71c" + "0" * 33
        records = [
            raw_commit(sha, "cora1022/project-a", "feat: 최신 제목", now),
            raw_commit(
                sha.upper(),
                "cora1022/project-a",
                "feat: 같은 커밋의 중복 이벤트",
                now - timedelta(minutes=1),
            ),
        ]

        result = normalize_and_filter_commits(records, base_config())

        self.assertEqual(len(result), 1)
        self.assertEqual(value(result[0], "sha").lower(), sha)

    def test_normalization_excludes_commits_with_two_parents(self) -> None:
        now = datetime(2026, 8, 21, 6, tzinfo=UTC)
        merge = raw_commit(
            "7" * 40,
            "cora1022/project-a",
            "feat: 부모가 둘인 병합 커밋",
            now,
        )
        merge["parents"] = [{"sha": "parent-a"}, {"sha": "parent-b"}]
        ordinary = raw_commit(
            "8" * 40,
            "cora1022/project-a",
            "feat: 부모가 하나인 일반 커밋",
            now - timedelta(minutes=1),
        )
        ordinary["parents"] = [{"sha": "parent-a"}]

        result = normalize_and_filter_commits([merge, ordinary], base_config())

        self.assertEqual([value(item, "sha") for item in result], ["8" * 40])


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 21, 6, tzinfo=UTC)

    def test_selects_the_three_latest_commits_regardless_of_repository(self) -> None:
        commits = [
            raw_commit("a1", "cora1022/project-a", "feat: A 최신", self.now),
            raw_commit(
                "a2",
                "cora1022/project-a",
                "fix: A 다음",
                self.now - timedelta(minutes=1),
            ),
            raw_commit(
                "b1",
                "cora1022/project-b",
                "docs: B 최신",
                self.now - timedelta(minutes=2),
            ),
            raw_commit(
                "c1",
                "cora1022/project-c",
                "test: C 최신",
                self.now - timedelta(minutes=3),
            ),
        ]

        selected = select_commits(commits, 3)

        self.assertEqual([value(item, "sha") for item in selected], ["a1", "a2", "b1"])

    def test_orders_selected_commits_strictly_by_time(self) -> None:
        commits = [
            raw_commit("a1", "cora1022/project-a", "feat: A 최신", self.now),
            raw_commit(
                "a2",
                "cora1022/project-a",
                "fix: A 두 번째",
                self.now - timedelta(minutes=1),
            ),
            raw_commit(
                "b1",
                "cora1022/project-b",
                "docs: B 최신",
                self.now - timedelta(minutes=2),
            ),
            raw_commit(
                "a3",
                "cora1022/project-a",
                "test: A 세 번째",
                self.now - timedelta(minutes=3),
            ),
        ]

        selected = select_commits(commits, 3)

        self.assertEqual([value(item, "sha") for item in selected], ["a1", "a2", "b1"])

    def test_returns_all_available_commits_when_fewer_than_limit(self) -> None:
        commits = [
            raw_commit("a1", "cora1022/project-a", "feat: A", self.now),
            raw_commit(
                "b1",
                "cora1022/project-b",
                "fix: B",
                self.now - timedelta(minutes=1),
            ),
        ]

        selected = select_commits(commits, 3)

        self.assertEqual([value(item, "sha") for item in selected], ["a1", "b1"])

    def test_selects_six_latest_commits_for_the_grid(self) -> None:
        commits = [
            raw_commit(
                f"sha{index}",
                f"cora1022/project-{index}",
                f"feat: 작업 {index}",
                self.now - timedelta(minutes=index),
            )
            for index in range(7)
        ]

        selected = select_commits(commits, 6)

        self.assertEqual(
            [value(item, "sha") for item in selected],
            [f"sha{index}" for index in range(6)],
        )


class CollectionWindowTests(unittest.TestCase):
    def test_keeps_latest_window_when_three_commits_exist_and_drops_future(self) -> None:
        now = datetime(2026, 8, 21, 12, 0, tzinfo=KST)

        def search_item(
            sha_digit: str, repository: str, authored_at: datetime
        ) -> dict[str, Any]:
            return {
                "sha": sha_digit * 40,
                "repository": {
                    "full_name": repository,
                    "private": False,
                },
                "commit": {
                    "message": "feat: 공개 작업",
                    "author": {
                        "name": "Hwang Young-yeon",
                        "email": "cora1022@users.noreply.github.com",
                        "date": authored_at.astimezone(UTC).isoformat(),
                    },
                    "committer": {
                        "name": "Hwang Young-yeon",
                        "email": "cora1022@users.noreply.github.com",
                    },
                },
                "author": {"login": "cora1022"},
                "committer": {"login": "cora1022"},
                "parents": [{"sha": "parent"}],
            }

        recent = [
            search_item("a", "cora1022/project-a", now - timedelta(hours=1)),
            search_item("b", "cora1022/project-a", now - timedelta(days=1)),
            search_item("c", "cora1022/project-a", now - timedelta(days=2)),
            search_item("f", "cora1022/project-a", now + timedelta(hours=1)),
        ]
        with patch(
            "scripts.generate_recent_commits._search_interval",
            return_value=recent,
        ) as mocked_search:
            commits = collect_public_commits(object(), base_config(), now)

        selected = select_commits(commits, 3)
        self.assertEqual(
            [value(item, "repository") for item in selected],
            ["cora1022/project-a", "cora1022/project-a", "cora1022/project-a"],
        )
        self.assertNotIn("f" * 40, [value(item, "sha") for item in commits])
        self.assertEqual(mocked_search.call_count, 1)


class KoreanTimeTests(unittest.TestCase):
    def test_relative_time_uses_korean_labels(self) -> None:
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)

        self.assertEqual(
            relative_time(now - timedelta(hours=2), now, KST), "2시간 전"
        )
        self.assertEqual(
            relative_time(now - timedelta(hours=25), now, KST), "어제"
        )
        self.assertEqual(
            relative_time(now - timedelta(days=3), now, KST), "3일 전"
        )

    def test_streak_starts_today_or_yesterday_and_counts_kst_dates(self) -> None:
        now = datetime(2026, 8, 21, 0, 30, tzinfo=KST)
        today_utc = datetime(2026, 8, 20, 15, 10, tzinfo=UTC)  # 8/21 00:10 KST
        yesterday_utc = datetime(2026, 8, 20, 4, 0, tzinfo=UTC)  # 8/20 13:00 KST
        two_days_ago_utc = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)

        self.assertEqual(
            calculate_streak(
                [today_utc, yesterday_utc, two_days_ago_utc], now, KST
            ),
            3,
        )
        self.assertEqual(
            calculate_streak([yesterday_utc, two_days_ago_utc], now, KST), 2
        )
        self.assertEqual(
            calculate_streak([two_days_ago_utc], now, KST), 0
        )

    def test_stats_days_uses_kst_calendar_boundaries_not_a_rolling_window(self) -> None:
        now = datetime(2026, 8, 21, 0, 30, tzinfo=KST)

        def commit_at(sha_digit: str, repository: str, local_time: datetime) -> Commit:
            return Commit(
                sha=sha_digit * 40,
                repository=repository,
                message="feat: 통계 경계 테스트",
                title="통계 경계 테스트",
                commit_type="FEAT",
                authored_at=local_time.astimezone(UTC),
                url=f"https://github.com/{repository}/commit/{sha_digit * 40}",
                language="Python",
                author_login="cora1022",
                author_name="Hwang Young-yeon",
                author_email="cora1022@users.noreply.github.com",
            )

        commits = [
            # Seven KST calendar dates includes 8/15 through 8/21, inclusive.
            commit_at("a", "cora1022/first-day", datetime(2026, 8, 15, 0, 0, tzinfo=KST)),
            # This is only one minute earlier, but belongs to the excluded KST date.
            commit_at("b", "cora1022/too-old", datetime(2026, 8, 14, 23, 59, tzinfo=KST)),
            commit_at("c", "cora1022/today", datetime(2026, 8, 21, 0, 20, tzinfo=KST)),
            # A same-date commit after now must not be counted yet.
            commit_at("d", "cora1022/future", datetime(2026, 8, 21, 0, 31, tzinfo=KST)),
        ]

        stats = calculate_stats(commits, now, KST, stats_days=7)

        self.assertEqual(stats["commit_count"], 2)
        self.assertEqual(stats["repository_count"], 2)
        self.assertEqual(stats["streak"], 1)

        future_only = calculate_stats([commits[-1]], now, KST, stats_days=7)
        self.assertEqual(future_only["commit_count"], 0)
        self.assertEqual(future_only["streak"], 0)


class TextSafetyTests(unittest.TestCase):
    def test_truncate_preserves_short_text_and_unicode_boundaries(self) -> None:
        self.assertEqual(truncate_text("짧은 글", 20), "짧은 글")

        original = "한글과 Unicode 🚀 커밋 메시지"
        truncated = truncate_text(original, 10)
        self.assertTrue(truncated.endswith("…"))
        self.assertTrue(original.startswith(truncated[:-1]))
        self.assertNotIn("\ufffd", truncated)
        self.assertNotRegex(truncated[:-1], r"[\uD800-\uDFFF]$")

        # A combining mark must stay attached to its base character.
        combined = "A\u0301BCDE"
        combined_result = truncate_text(combined, 3)
        self.assertTrue(combined_result.startswith("A\u0301"))
        self.assertFalse(bool(combined_result[:-1]) and re.match(
            r"[\u0300-\u036f]", combined_result[0]
        ))

        # A zero-width-joiner emoji sequence must remain one visual cluster.
        self.assertEqual(truncate_text("👩‍💻AB", 3), "👩‍💻…")

    def test_xml_escape_makes_untrusted_text_safe_for_svg(self) -> None:
        escaped = xml_escape('<개발 & "테스트" \'기록\'>')

        self.assertIn("&lt;", escaped)
        self.assertIn("&gt;", escaped)
        self.assertIn("&amp;", escaped)
        self.assertIn("&quot;", escaped)
        self.assertTrue("&apos;" in escaped or "&#x27;" in escaped)
        self.assertNotIn("<개발", escaped)
        # Parsing the result as SVG text catches malformed/double-escaped XML.
        node = ET.fromstring(f'<text data-value="{escaped}">{escaped}</text>')
        self.assertEqual(node.text, '<개발 & "테스트" \'기록\'>')

        control_escaped = xml_escape("앞\x01뒤")
        control_node = ET.fromstring(f"<text>{control_escaped}</text>")
        self.assertEqual(control_node.text, "앞�뒤")

    def test_rendered_commit_svg_parses_with_untrusted_title_and_language(self) -> None:
        malicious_title = '제목 <script>alert("x")</script> & \'인용\''
        malicious_language = 'C++ & <svg> "언어"'
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)
        commit = Commit(
            sha="a92f71c" + "0" * 33,
            repository="cora1022/x</title><script>repo</script>",
            message=f"feat: {malicious_title}",
            title=malicious_title,
            commit_type="FEAT",
            authored_at=now - timedelta(hours=2),
            url="https://github.com/cora1022/safe-repository/commit/"
            + "a92f71c"
            + "0" * 33,
            language=malicious_language,
            author_login="cora1022",
            author_name="Hwang Young-yeon",
            author_email="cora1022@users.noreply.github.com",
        )

        svg = render_commit_svg(commit, now, KST)
        document = ET.parse(io.StringIO(svg))
        rendered_text = "".join(document.getroot().itertext())

        self.assertIn(malicious_title, rendered_text)
        self.assertIn(malicious_language, rendered_text)
        self.assertNotIn("<script>", svg)
        self.assertNotIn("<svg>", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertIn("&lt;svg&gt;", svg)

    def test_commit_card_uses_compact_doodle_canvas(self) -> None:
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)
        commit = Commit(
            sha="a92f71c" + "0" * 33,
            repository="cora1022/project",
            message="feat: 최신 작업",
            title="최신 작업",
            commit_type="FEAT",
            authored_at=now,
            url="https://github.com/cora1022/project/commit/" + "a92f71c" + "0" * 33,
            language="Python",
            author_login="cora1022",
            author_name="Hwang Young-yeon",
            author_email="cora1022@users.noreply.github.com",
        )

        svg = render_commit_svg(commit, now, KST)

        self.assertIn('viewBox="0 0 420 148"', svg)
        self.assertIn('fill="#FFFFFF" stroke="#24292F" stroke-width="1.75"', svg)
        self.assertIn('M13 14 C10 45 14 95 12 134', svg)
        self.assertIn('stroke-dasharray="5 6"', svg)
        self.assertIn('M45 34 C60 34 71 44 72 60', svg)

    def test_commit_type_icons_use_named_doodle_symbols(self) -> None:
        chore = _commit_type_icon_svg("CHORE")
        perf = _commit_type_icon_svg("PERF")
        commit = _commit_type_icon_svg("COMMIT")

        self.assertIn('data-commit-icon="chore"', chore)
        self.assertIn('data-commit-icon="perf"', perf)
        self.assertIn('data-commit-icon="commit"', commit)
        self.assertIn('M47 65 L56 60', chore)
        self.assertIn('M33 68 C32 57', perf)
        self.assertIn('circle cx="52" cy="67"', commit)
        for icon in (chore, perf, commit):
            ET.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg">{icon}</svg>')

    def test_header_uses_simplified_korean_title(self) -> None:
        svg = render_header_svg(
            {"stats_days": 7, "commit_count": 3, "repository_count": 2, "streak": 1}
        )

        self.assertIn("요즘 커밋한거", svg)
        self.assertIn('viewBox="0 0 720 84"', svg)
        self.assertIn('x="61" y="60" font-size="36"', svg)
        self.assertIn('d="M63 74 C109 70 158 77 210 72', svg)
        self.assertIn(
            'transform="translate(-2 3) scale(1.25) rotate(-4 20 20)"', svg
        )
        self.assertIn('fill="#24292F" stroke="#24292F" stroke-width="2.4"', svg)
        self.assertIn('circle cx="15" cy="23" r="1.8" fill="#FFFFFF"', svg)
        self.assertIn("검정 깃허브 고양이 낙서", svg)
        self.assertNotIn('fill="#C9F1EE"', svg)
        self.assertNotIn('M10 5 L7 1 M35 5 L39 1', svg)
        self.assertNotIn("RECENT PUBLIC COMMITS", svg)
        self.assertNotIn("BUILDING NOW", svg)
        self.assertNotIn("연필 아이콘", svg)
        self.assertNotIn("3 commits", svg)
        self.assertNotIn("2 repos", svg)
        self.assertNotIn("1 days", svg)
        self.assertNotIn("최근 7일", svg)
        self.assertNotIn("활성 저장소", svg)
        self.assertNotIn("연속 활동", svg)

    def test_empty_card_keeps_doodle_outline(self) -> None:
        svg = render_empty_svg()

        self.assertIn('fill="#FFFFFF" stroke="#24292F" stroke-width="2.2"', svg)


class ReadmeReplacementTests(unittest.TestCase):
    def test_updates_borderless_float_slots_without_rebuilding_projects(self) -> None:
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)
        commits = [
            Commit(
                sha=f"{index}" * 40,
                repository=f"cora1022/project-{index}",
                message=f"feat: 작업 {index}",
                title=f"작업 {index}",
                commit_type="FEAT",
                authored_at=now - timedelta(minutes=index),
                url=f"https://github.com/cora1022/project-{index}/commit/{index * 40}",
                language="Python",
                author_login="cora1022",
                author_name="Hwang Young-yeon",
                author_email="cora1022@users.noreply.github.com",
            )
            for index in range(1, 4)
        ]
        original = (
            "before\n"
            "<!-- RECENT_COMMITS_START -->\n"
            "<div>\n"
            '  <a href="featured"><img src="./assets/featured-projects/project.svg" '
            'width="54%" align="left"></a>\n'
            "<!-- RECENT_COMMIT_1_START -->\nold 1\n<!-- RECENT_COMMIT_1_END -->\n"
            "<!-- RECENT_COMMIT_2_START -->\nold 2\n<!-- RECENT_COMMIT_2_END -->\n"
            "<!-- RECENT_COMMIT_3_START -->\nold 3\n<!-- RECENT_COMMIT_3_END -->\n"
            '<br clear="all">\n'
            "</div>\n"
            "<!-- RECENT_COMMITS_END -->\n"
            "after\n"
        )

        self.assertTrue(has_readme_slots(original))
        result = replace_readme_slots(original, commits)

        self.assertTrue(result.startswith("before\n"))
        self.assertTrue(result.endswith("after\n"))
        self.assertIn('href="featured"', result)
        self.assertIn('<br clear="all">', result)
        self.assertNotIn("old 1", result)
        for index in range(1, 4):
            self.assertIn(f"project-{index}/commit/{index * 40}", result)
            self.assertIn(f'commit-{index}.svg" width="41%" align="right"', result)

    def test_float_slots_render_only_available_cards(self) -> None:
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)
        commit = Commit(
            sha="a" * 40,
            repository="cora1022/project",
            message="fix: 작업",
            title="작업",
            commit_type="FIX",
            authored_at=now,
            url="https://github.com/cora1022/project/commit/" + "a" * 40,
            language="Python",
            author_login="cora1022",
            author_name="Hwang Young-yeon",
            author_email="cora1022@users.noreply.github.com",
        )
        original = (
            "<!-- RECENT_COMMITS_START -->\n"
            "<!-- RECENT_COMMIT_1_START -->x<!-- RECENT_COMMIT_1_END -->\n"
            "<!-- RECENT_COMMIT_2_START -->x<!-- RECENT_COMMIT_2_END -->\n"
            "<!-- RECENT_COMMIT_3_START -->x<!-- RECENT_COMMIT_3_END -->\n"
            "<!-- RECENT_COMMITS_END -->"
        )

        result = replace_readme_slots(original, [commit])

        self.assertEqual(result.count("<a href="), 1)
        self.assertIn("commit-1.svg", result)
        self.assertNotIn("commit-2.svg", result)
        self.assertNotIn("commit-3.svg", result)

    def test_float_slots_render_empty_state_without_link(self) -> None:
        original = (
            "<!-- RECENT_COMMITS_START -->\n"
            "<!-- RECENT_COMMIT_1_START -->x<!-- RECENT_COMMIT_1_END -->\n"
            "<!-- RECENT_COMMIT_2_START -->x<!-- RECENT_COMMIT_2_END -->\n"
            "<!-- RECENT_COMMIT_3_START -->x<!-- RECENT_COMMIT_3_END -->\n"
            "<!-- RECENT_COMMITS_END -->"
        )

        result = replace_readme_slots(original, [])

        self.assertNotIn("<a href=", result)
        self.assertEqual(result.count("commit-1.svg"), 1)
        self.assertNotIn("commit-2.svg", result)
        self.assertNotIn("commit-3.svg", result)

    def test_any_partial_float_slot_forces_safe_slot_validation(self) -> None:
        malformed = (
            "<!-- RECENT_COMMITS_START -->\n"
            "<!-- RECENT_COMMIT_2_START -->x<!-- RECENT_COMMIT_2_END -->\n"
            "<!-- RECENT_COMMITS_END -->"
        )

        self.assertTrue(has_readme_slots(malformed))
        with self.assertRaisesRegex(GeneratorError, "1번 슬롯"):
            replace_readme_slots(malformed, [])

    def test_float_slots_reject_out_of_order_markers(self) -> None:
        malformed = (
            "<!-- RECENT_COMMITS_START -->\n"
            "<!-- RECENT_COMMIT_2_START -->x<!-- RECENT_COMMIT_2_END -->\n"
            "<!-- RECENT_COMMIT_1_START -->x<!-- RECENT_COMMIT_1_END -->\n"
            "<!-- RECENT_COMMIT_3_START -->x<!-- RECENT_COMMIT_3_END -->\n"
            "<!-- RECENT_COMMITS_END -->"
        )

        with self.assertRaisesRegex(GeneratorError, "1~3번 순서"):
            replace_readme_slots(malformed, [])

    def test_builds_three_cards_as_one_vertical_column(self) -> None:
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)
        commits = [
            Commit(
                sha=f"{index}" * 40,
                repository=f"cora1022/project-{index}",
                message=f"feat: 작업 {index}",
                title=f"작업 {index}",
                commit_type="FEAT",
                authored_at=now - timedelta(minutes=index),
                url=f"https://github.com/cora1022/project-{index}/commit/{index * 40}",
                language="Python",
                author_login="cora1022",
                author_name="Hwang Young-yeon",
                author_email="cora1022@users.noreply.github.com",
            )
            for index in range(1, 4)
        ]

        block = build_readme_block(commits)

        self.assertEqual(block.count("<a href="), 3)
        self.assertEqual(block.count('width="96%"'), 3)
        self.assertEqual(block.count("<br>"), 3)
        self.assertIn('header.svg" width="100%"', block)
        for index in range(1, 4):
            self.assertIn(f"commit-{index}.svg", block)
        self.assertNotIn("commit-4.svg", block)

    def test_builds_only_available_cards_in_vertical_column(self) -> None:
        now = datetime(2026, 8, 21, 15, 0, tzinfo=KST)
        commits = [
            Commit(
                sha=f"{index}" * 40,
                repository=f"cora1022/project-{index}",
                message=f"fix: 작업 {index}",
                title=f"작업 {index}",
                commit_type="FIX",
                authored_at=now - timedelta(minutes=index),
                url=f"https://github.com/cora1022/project-{index}/commit/{index * 40}",
                language="Python",
                author_login="cora1022",
                author_name="Hwang Young-yeon",
                author_email="cora1022@users.noreply.github.com",
            )
            for index in range(1, 3)
        ]

        block = build_readme_block(commits)

        self.assertEqual(block.count("<a href="), 2)
        self.assertIn("commit-1.svg", block)
        self.assertIn("commit-2.svg", block)
        self.assertNotIn("commit-3.svg", block)

    def test_empty_vertical_column_has_no_broken_link(self) -> None:
        block = build_readme_block([])

        self.assertNotIn("<a href=", block)
        self.assertIn('commit-1.svg" width="96%"', block)
        self.assertIn("최근 공개 커밋 없음", block)

    def test_replaces_only_marker_range_and_preserves_outside_bytes(self) -> None:
        start_marker = "<!-- RECENT_COMMITS_START -->"
        end_marker = "<!-- RECENT_COMMITS_END -->"
        original = (
            "# 소개\r\n"
            "그대로 남아야 하는 첫 영역\r\n\r\n"
            f"{start_marker}\r\n"
            "old generated content\r\n"
            f"{end_marker}\r\n\r\n"
            "## 관심 분야\r\n"
            "이 영역도 그대로 남아야 합니다.\r\n"
        )
        block = (
            f"{start_marker}\n\n"
            '<img src="./assets/recent-commits/header.svg" width="100%">\n\n'
            f"{end_marker}"
        )

        result = replace_readme_section(original, block)

        original_start = original.index(start_marker)
        original_end = original.index(end_marker) + len(end_marker)
        result_start = result.index(start_marker)
        result_end = result.index(end_marker) + len(end_marker)
        self.assertEqual(result[:result_start], original[:original_start])
        self.assertEqual(result[result_end:], original[original_end:])
        self.assertEqual(result[result_start:result_end], block)
        self.assertEqual(result.count(start_marker), 1)
        self.assertEqual(result.count(end_marker), 1)
        self.assertNotIn("old generated content", result)

    def test_replaces_recent_column_without_touching_featured_column_table(self) -> None:
        start_marker = "<!-- RECENT_COMMITS_START -->"
        end_marker = "<!-- RECENT_COMMITS_END -->"
        original = (
            '<table width="100%">\n<tr>\n<td width="55%">\n'
            '<!-- FEATURED_PROJECTS_START -->\nfeatured\n'
            '<!-- FEATURED_PROJECTS_END -->\n</td>\n<td width="45%">\n'
            f"{start_marker}\nold recent\n{end_marker}\n"
            "</td>\n</tr>\n</table>\n\n### 기존 프로젝트 설명\n"
        )
        block = (
            f"{start_marker}\n\n"
            '<div align="center">new recent</div>\n\n'
            f"{end_marker}"
        )

        result = replace_readme_section(original, block)

        self.assertIn("<!-- FEATURED_PROJECTS_START -->\nfeatured\n", result)
        self.assertIn('<td width="55%">', result)
        self.assertIn('<td width="45%">', result)
        self.assertIn("### 기존 프로젝트 설명", result)
        self.assertNotIn("old recent", result)
        self.assertIn(block, result)

    def test_inserts_missing_markers_after_primary_links_before_interests(self) -> None:
        start_marker = "<!-- RECENT_COMMITS_START -->"
        end_marker = "<!-- RECENT_COMMITS_END -->"
        original = (
            "# cora1022\n\n"
            "안녕하세요. 직접 만들며 배우는 개발자입니다.\n\n"
            "### 주요 링크\n"
            '<a href="https://example.com/portfolio">포트폴리오</a>\n\n'
            "## 관심 있는거\n\n"
            "웹 보안과 로봇을 공부하고 있습니다.\n"
        )
        block = (
            f"{start_marker}\n\n"
            '<img src="./assets/recent-commits/header.svg" width="100%">\n\n'
            f"{end_marker}"
        )

        result = replace_readme_section(original, block)

        self.assertEqual(result.count(start_marker), 1)
        self.assertEqual(result.count(end_marker), 1)
        self.assertLess(result.index("포트폴리오</a>"), result.index(start_marker))
        self.assertLess(result.index(end_marker), result.index("## 관심 있는거"))
        self.assertEqual(result.replace(block + "\n\n", ""), original)

    def test_inserts_missing_markers_before_interests_svg_header(self) -> None:
        start_marker = "<!-- RECENT_COMMITS_START -->"
        end_marker = "<!-- RECENT_COMMITS_END -->"
        interests_header = (
            '<div><img src="./assets/section-headers/interests.svg" '
            'width="53%" alt="관심 있는거"></div>'
        )
        original = (
            "# cora1022\n\n"
            "안녕하세요. 직접 만들며 배우는 개발자입니다.\n\n"
            f"{interests_header}\n\n"
            "웹 보안과 로봇을 공부하고 있습니다.\n"
        )
        block = (
            f"{start_marker}\n\n"
            '<img src="./assets/recent-commits/header.svg" width="100%">\n\n'
            f"{end_marker}"
        )

        result = replace_readme_section(original, block)

        self.assertLess(result.index(end_marker), result.index(interests_header))
        self.assertEqual(result.replace(block + "\n\n", ""), original)


class FailureSafetyTests(unittest.TestCase):
    def test_api_failure_leaves_existing_readme_and_svg_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "recent-commits.config.json"
            readme_path = root / "README.md"
            output_dir = root / "assets" / "recent-commits"
            output_dir.mkdir(parents=True)
            config_path.write_text(
                json.dumps(base_config(), ensure_ascii=False), encoding="utf-8"
            )
            original_readme = b"# profile\n\nexisting content\n"
            original_svg = b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n"
            readme_path.write_bytes(original_readme)
            (output_dir / "header.svg").write_bytes(original_svg)

            with patch(
                "scripts.generate_recent_commits.collect_public_commits",
                side_effect=GitHubAPIError("simulated network failure"),
            ), redirect_stderr(io.StringIO()):
                result = main(
                    [
                        "--config",
                        str(config_path),
                        "--readme",
                        str(readme_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(result, 1)
            self.assertEqual(readme_path.read_bytes(), original_readme)
            self.assertEqual((output_dir / "header.svg").read_bytes(), original_svg)


if __name__ == "__main__":
    unittest.main()
