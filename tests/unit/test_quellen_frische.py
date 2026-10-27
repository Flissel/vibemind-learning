"""Source freshness: a watched page counts as changed only when its visible text changes."""

from __future__ import annotations

from spaces.learning.services.quellen.frische import Change, Watch, check, text_digest

PAGE = """<html><head><title>Claude Code installieren</title>
<script>window.track = 1;</script><style>.a{color:red}</style></head>
<body><nav>Anzeige: Jetzt kaufen</nav>
<h1>Claude Code installieren</h1>
<p>Führen Sie   <code>irm https://claude.ai/install.ps1 | iex</code> aus.</p>
</body></html>"""


def _watch(url: str = "https://docs.example/install", sha: str | None = None) -> Watch:
    return Watch(id="w1", course_id="c1", url=url, sha256=sha if sha is not None else text_digest(PAGE))


def test_unchanged_page_is_no_change() -> None:
    assert check([_watch()], lambda url: PAGE) == []


def test_whitespace_script_and_style_changes_do_not_count() -> None:
    noisy = PAGE.replace("<script>window.track = 1;</script>", "<script>window.track = 2; var x = 'neu';</script>") \
        .replace("Führen Sie   <code>", "Führen Sie <code>").replace(".a{color:red}", ".a{color:blue}")
    assert check([_watch()], lambda url: noisy) == []


def test_a_text_change_is_reported_with_both_digests() -> None:
    changed = PAGE.replace("install.ps1", "install.exe")
    changes = check([_watch()], lambda url: changed)
    assert changes == [Change(course_id="c1", url="https://docs.example/install", old_sha=text_digest(PAGE), new_sha=text_digest(changed))]
    assert changes[0].old_sha != changes[0].new_sha


def test_fetch_failures_are_skipped_not_reported() -> None:
    def fetch(url: str) -> str:
        raise OSError("down")

    assert check([_watch()], fetch) == []
