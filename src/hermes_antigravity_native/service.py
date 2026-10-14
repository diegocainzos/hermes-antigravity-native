"""Credential + project orchestration shared by the gateway, doctor and CLI."""

from __future__ import annotations

import threading
from typing import Any

from . import auth
from .client import AntigravityClient, resolve_project_id

_client: AntigravityClient | None = None
_client_lock = threading.Lock()


def shared_client() -> AntigravityClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AntigravityClient()
        return _client


def resolve_session(*, force_refresh: bool = False) -> tuple[auth.Credentials, str]:
    """Return (fresh credentials, project id); remembers a newly discovered project id."""
    client = shared_client()
    creds = auth.get_valid_credentials(force_refresh=force_refresh)
    warmed = None if creds.project_id else client.load_code_assist(creds.access)
    project = resolve_project_id(credential_project_id=creds.project_id, warmed=warmed, email=creds.email)
    if creds.project_id != project and warmed:
        creds.project_id = project
        auth.remember(creds)  # own source only: native store or fingerprint cache
    return creds, project


def catalog_ids(refresh: bool = False) -> list[str]:
    """Model ids to offer in the picker: static ``agy models`` list (+ live extras on refresh)."""
    from .models import default_model_ids
    from .client import usable_model_ids

    ids = default_model_ids()
    if refresh:
        creds, project = resolve_session()
        live = usable_model_ids(shared_client().fetch_available_models(creds.access, project))
        ids = ids + [m for m in live if m not in ids]
    return ids


def describe(creds: auth.Credentials) -> dict[str, Any]:
    return {"source": creds.source, "has_access": bool(creds.access), "fresh": creds.is_fresh()}
