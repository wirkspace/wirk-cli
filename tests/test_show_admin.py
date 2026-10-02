"""wirk show makes one link; wirk admin is a person's command and sends exactly what it was given."""

import json

from conftest import envelope


def test_show_status_sends_the_preset_and_the_timezone(run, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/London")
    code, out, err, fake = run(["show", "status", "--no-open"],
                               lambda r: envelope("https://show.wirk.life/v/abc\nWhat is waiting on you"))
    assert code == 0 and fake.requests[0].url.path == "/v2/show"
    assert fake.bodies() == [{"preset": "status", "timezone": "Europe/London", "format": "text"}]
    assert out.startswith("https://show.wirk.life/v/abc")


def test_show_from_a_file_and_revoke(run, tmp_path, monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr("wirk_cli.cli.local_zone", lambda: None)
    (tmp_path / "view.json").write_text(json.dumps({"title": "Launch", "blocks": [{"type": "list"}]}))
    code, out, err, fake = run(["show", "--file", "view.json", "--no-open"])
    assert fake.bodies() == [{"title": "Launch", "blocks": [{"type": "list"}], "format": "text"}]
    code, out, err, fake = run(["show", "--revoke", "https://show.wirk.life/v/abc"])
    assert fake.bodies() == [{"revoke": "https://show.wirk.life/v/abc", "format": "text"}]


def test_show_takes_exactly_one_mode_and_is_never_resent(run):
    code, out, err, fake = run(["show", "status", "--revoke", "x"])
    assert code == 2 and fake.requests == []
    import httpx

    def timeout(request):
        raise httpx.ReadTimeout("slow")
    code, out, err, fake = run(["show", "status", "--no-open"], timeout)
    assert code == 1 and len(fake.requests) == 1


def test_a_browser_that_does_not_open_still_succeeds(run, monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    code, out, err, fake = run(["show", "status"], lambda r: envelope("https://show.wirk.life/v/abc\nx"))
    assert code == 0 and "open the link above" in err
