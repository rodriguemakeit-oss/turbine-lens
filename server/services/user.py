"""User identity helpers."""

from __future__ import annotations

import logging

from fastapi import Request

logger = logging.getLogger(__name__)


def get_current_user(request: Request) -> str:
    """Extract user email from forwarded headers."""
    email = request.headers.get("x-forwarded-email")
    if email:
        return email
    return "unknown@turbine_lens"


def get_current_user_name(request: Request) -> str | None:
    """Extract display name from forwarded headers."""
    return request.headers.get("x-forwarded-preferred-username")


def get_fmapi_credentials() -> tuple[str, str]:
    """Mint FMAPI host + bearer token from the app service principal."""
    from databricks.sdk import WorkspaceClient
    from ..config import get_workspace_host

    w = WorkspaceClient()
    host = (w.config.host or get_workspace_host()).rstrip("/")
    headers = w.config.authenticate() or {}
    token = headers.get("Authorization", "").split(" ", 1)[-1]
    return host, token
