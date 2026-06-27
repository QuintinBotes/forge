"""Linear auth header construction (OAuth bearer + personal API key).

Linear's personal API keys are sent as the raw ``Authorization`` value (no
``Bearer`` prefix), while OAuth access tokens use ``Bearer``. Secrets live in the
F37 vault; this module only builds the header.
"""

from __future__ import annotations


def api_key_header(api_key: str) -> str:
    return api_key


def bearer_header(access_token: str) -> str:
    return f"Bearer {access_token}"


__all__ = ["api_key_header", "bearer_header"]
