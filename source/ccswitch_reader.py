"""Read CC Switch 4.0.5 Codex providers without touching live configuration.

Only ``load_provider`` returns a secret, in memory, for the explicitly selected
API provider. Callers must never print/log its return value. OAuth fields are
excluded in SQL, including when CC Switch has stashed them on an API provider.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tomllib
from urllib.parse import urlsplit


class ProviderError(ValueError):
    """A provider cannot be used by the isolated API instance."""


def _connect(cc_root: Path) -> sqlite3.Connection:
    database = Path(cc_root).expanduser().resolve() / "cc-switch.db"
    # mode=ro prevents database creation and all application writes. Do not use
    # immutable=1: the running CC Switch process may have committed WAL changes.
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _parse_config(value: object) -> dict:
    if not isinstance(value, str):
        raise ProviderError("The selected provider has no TOML configuration.")
    try:
        return tomllib.loads(value)
    except tomllib.TOMLDecodeError:
        # The original parser exception can include secret-bearing source text.
        raise ProviderError("The provider TOML configuration is invalid.") from None


def list_providers(cc_root: Path) -> list[dict]:
    """Return non-secret metadata; do not retrieve any provider's auth object.

    ``kind`` is ``official``, ``api_key``, or ``unsupported``. Endpoint metadata
    includes the hostname only, excluding credentials, paths, and query strings.
    """
    connection = _connect(cc_root)
    try:
        rows = connection.execute(
            """SELECT id, name, is_current, category,
                      json_extract(settings_config, '$.config'),
                      CASE WHEN json_type(settings_config, '$.auth.OPENAI_API_KEY') = 'text'
                            AND length(trim(json_extract(settings_config, '$.auth.OPENAI_API_KEY'))) > 0
                           THEN 1 ELSE 0 END
               FROM providers WHERE app_type = 'codex'
               ORDER BY sort_index, created_at, id"""
        ).fetchall()
    finally:
        connection.close()

    result = []
    for provider_id, name, is_current, category, config_text, has_api_key in rows:
        config = _parse_config(config_text)
        official = category == "official" or provider_id == "codex-official"
        provider_name = config.get("model_provider", "openai")
        provider = config.get("model_providers", {}).get(provider_name, {})
        endpoint = provider.get("base_url")
        try:
            endpoint_host = urlsplit(endpoint).hostname if isinstance(endpoint, str) else None
        except ValueError:
            raise ProviderError("The provider endpoint URL is invalid.") from None
        result.append(
            {
                "id": provider_id,
                "name": name,
                "model": config.get("model"),
                "endpoint_host": endpoint_host,
                "is_current": bool(is_current),
                "kind": "official" if official else "api_key" if has_api_key else "unsupported",
            }
        )
    return result


def load_provider(cc_root: Path, provider_id: str) -> dict:
    """Return selected API provider config and API-only auth, never OAuth.

    The original TOML is returned for the caller to merge deliberately. No live
    files are written. Official or missing-key providers are rejected instead of
    silently falling back to the user's subscription or environment credentials.
    """
    connection = _connect(cc_root)
    try:
        metadata = connection.execute(
            "SELECT category FROM providers WHERE app_type = 'codex' AND id = ?",
            (provider_id,),
        ).fetchone()
        if metadata is None:
            raise ProviderError("The selected Codex provider was not found.")
        if metadata[0] == "official" or provider_id == "codex-official":
            raise ProviderError("Official subscription providers cannot be imported into the API instance.")
        row = connection.execute(
            """SELECT json_extract(settings_config, '$.config'),
                      json_extract(settings_config, '$.auth.OPENAI_API_KEY')
               FROM providers WHERE app_type = 'codex' AND id = ?""",
            (provider_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ProviderError("The selected Codex provider was removed while reading it.")
    config_text, api_key = row
    _parse_config(config_text)
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderError("The selected provider has no API key stored in CC Switch.")
    return {
        "config_toml": config_text,
        "auth": {"auth_mode": "apikey", "OPENAI_API_KEY": api_key},
    }
