import pytest

from hermes_antigravity_native.security import assert_safe_api_base_url, mask_email, redact_secrets, resolve_callback_host


def test_redacts_tokens():
    s = 'Bearer ya29.A0AbcDEF-ghi_jkl and {"refresh_token": "1//0gAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "access":"ya29.zzz"} client_secret=GOCSPX-abc&x=1'
    out = redact_secrets(s)
    for leak in ("ya29.A0Ab", "1//0gAAAA", "ya29.zzz", "GOCSPX-abc"):
        assert leak not in out
    assert "[redacted" in out


def test_mask_email():
    assert mask_email("gaiosoxd@gmail.com") == "g***d@gmail.com"
    assert mask_email("ab@x.io") == "a***@x.io"
    assert mask_email(None) is None
    assert mask_email("nope") == "[redacted-email]"


def test_base_url_allowlist():
    assert assert_safe_api_base_url("https://daily-cloudcode-pa.googleapis.com/") == "https://daily-cloudcode-pa.googleapis.com"
    for bad in ("http://x.googleapis.com", "https://evil.com", "https://u:p@x.googleapis.com", "https://googleapis.com.evil.com"):
        with pytest.raises(ValueError):
            assert_safe_api_base_url(bad)


def test_callback_host_loopback_only():
    assert resolve_callback_host("localhost") == "127.0.0.1"
    with pytest.raises(ValueError):
        resolve_callback_host("0.0.0.0")
