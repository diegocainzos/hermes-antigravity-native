from datetime import datetime, timedelta, timezone

from hermes_antigravity_native import cli, doctor, usage


def test_format_reset():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    iso = lambda d: (now + d).isoformat().replace("+00:00", "Z")
    assert usage.format_reset(iso(timedelta(minutes=45)), now) == "45m"
    assert usage.format_reset(iso(timedelta(hours=3, minutes=5)), now) == "3h 5m"
    assert usage.format_reset(iso(timedelta(days=2, hours=1)), now) == "2d 1h"
    assert usage.format_reset(iso(-timedelta(minutes=1)), now) == "now"
    assert usage.format_reset(None) == "n/a"


def test_quota_summary_parse_and_format():
    g = usage.parse_quota_summary({"groups": [{"displayName": "Gemini", "buckets": [
        {"bucketId": "5h", "displayName": "5 hour", "remainingFraction": 0.25, "resetTime": "bad"},
        {"remainingFraction": "x"}]}]})
    assert g[0]["buckets"][0]["remaining"] == 0.25 and len(g[0]["buckets"]) == 1
    out = usage.format_quota(g)
    assert "Gemini" in out and "25.0% left" in out and "[#####---------------]" in out
    assert usage.format_quota([]) == "No quota groups returned."


def test_parse_models_filters_internal():
    rows = usage.parse_models({"models": {"b": {"displayName": "B", "quotaInfo": {"remainingFraction": 2}}, "a": {}, "chat_1": {}, "i": {"isInternal": True}}})
    assert [r["id"] for r in rows] == ["a", "b"] and rows[1]["remaining"] == 1.0


def test_doctor_offline_no_creds_fails_without_network():
    checks = doctor.run_doctor(live=False)
    by = {c.name: c for c in checks}
    assert by["credentials"].status == "FAIL" and by["catalog"].status == "PASS" and by["live checks"].status == "SKIP"


def test_doctor_output_never_contains_secrets(_isolated_env):
    import json
    (_isolated_env / "auth" / "google_oauth.json").write_text(json.dumps({"access": "ya29.SUPERSECRET", "refresh": "1//0gREFRESHSECRETREFRESHSECRET", "email": "gaiosoxd@gmail.com", "expires": 1}))
    text = "\n".join(c.line() for c in doctor.run_doctor(live=False))
    assert "SUPERSECRET" not in text and "REFRESHSECRET" not in text and "gaiosoxd@" not in text
    assert "g***d@gmail.com" in text


def test_cli_status_exit_codes(capsys):
    import argparse
    ns = argparse.Namespace(ag_cmd="status")
    assert cli.handle_cli(ns) == 1
    assert "none" in capsys.readouterr().out


def test_cli_parser_wiring():
    import argparse
    p = argparse.ArgumentParser()
    cli.setup_cli(p)
    ns = p.parse_args(["doctor", "--offline"])
    assert ns.ag_cmd == "doctor" and ns.offline and ns.func is cli.handle_cli
