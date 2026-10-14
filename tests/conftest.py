import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Temp HERMES_HOME + no ambient Antigravity env, and no home-dir token file."""
    home = tmp_path / "hermes_home"
    (home / "auth").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for k in list(__import__("os").environ):
        if k.startswith(("ANTIGRAVITY_", "NOAGY_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("ANTIGRAVITY_AGY_TOKEN_FILE", str(tmp_path / "no-agy-token"))
    from hermes_antigravity_native import client, models

    client._project_cache.clear()
    models._dynamic_enums.clear()
    return home
