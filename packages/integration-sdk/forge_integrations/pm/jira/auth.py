"""Jira auth header construction (OAuth 3LO bearer + Basic email:API-token).

Token bundles live in the F37 vault; this module only turns a resolved secret
into an ``Authorization`` header and computes the Jira Cloud API base URL. The
full OAuth code-exchange/refresh dance is orchestrated by the API service layer
(parked here — see notes); these helpers are the pure, testable pieces.
"""

from __future__ import annotations

import base64

ATLASSIAN_API = "https://api.atlassian.com"


def basic_auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def bearer_header(access_token: str) -> str:
    return f"Bearer {access_token}"


def cloud_api_base(cloud_id: str) -> str:
    """OAuth tokens address the site via ``api.atlassian.com/ex/jira/{cloudid}``."""
    return f"{ATLASSIAN_API}/ex/jira/{cloud_id}"


__all__ = ["ATLASSIAN_API", "basic_auth_header", "bearer_header", "cloud_api_base"]
