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


# --- fetching: only public hosts, redirects checked hop by hop, size capped -----------

def test_private_loopback_and_metadata_addresses_are_never_fetched() -> None:
    from spaces.learning.services.quellen.cli import fetch, is_public_address

    for bad in ("127.0.0.1", "10.0.0.5", "192.168.0.7", "172.17.0.1", "169.254.169.254", "::1", "fd00::1", "0.0.0.0",
                "100.64.0.1", "::ffff:127.0.0.1", "2002:7f00:0001::1", "224.0.0.1",
                "64:ff9b::7f00:1", "64:ff9b:1::a00:5", "::7f00:1"):
        assert is_public_address(bad) is False, bad
    assert is_public_address("93.184.216.34") is True
    for url in ("http://127.0.0.1:8090/health", "http://localhost/x", "ftp://docs.example/x", "http://169.254.169.254/latest"):
        try:
            fetch(url)
        except ValueError:
            continue
        raise AssertionError(f"{url} was fetched")


def test_redirect_hops_are_checked_and_bodies_are_capped(monkeypatch) -> None:
    import httpx

    from spaces.learning.services.quellen import cli

    monkeypatch.setattr(cli, "resolve", lambda host: ["93.184.216.34"] if host == "docs.example" else ["93.184.216.34", "127.0.0.1"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"Location": "http://intern.local/secret"})
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (cli._MAX_BYTES + 10))
        return httpx.Response(200, text="<p>fein</p>")

    monkeypatch.setattr(cli, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert "fein" in fetch_via(cli, "http://docs.example/ok")
    for url in ("http://docs.example/hop", "http://docs.example/big"):
        try:
            fetch_via(cli, url)
        except ValueError:
            continue
        raise AssertionError(f"{url} was accepted")


def fetch_via(cli, url: str) -> str:
    return cli.fetch(url)
