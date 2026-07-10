"""GitHub Projects auth header — reuses HARD-01's GitHub App token machinery.

No new auth is implemented here: a GitHub App installation token (minted and
cached by :class:`forge_integrations.github_auth.InstallationTokenProvider`)
is simply turned into the ``Authorization`` header the GraphQL client sends.
"""

from __future__ import annotations

from forge_integrations.github_auth import InstallationTokenProvider


def bearer_header(access_token: str) -> str:
    return f"Bearer {access_token}"


def installation_auth_header(token_provider: InstallationTokenProvider) -> str:
    """Build the ``Authorization`` header from a live (cached/refreshing) App token.

    ``token_provider`` is the same :class:`InstallationTokenProvider` used by
    :class:`forge_integrations.github.GitHubClient` — no separate JWT/OAuth path
    is implemented for Projects v2.
    """
    return bearer_header(token_provider.token())


__all__ = ["bearer_header", "installation_auth_header"]
