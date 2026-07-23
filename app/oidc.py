"""OIDC configuration held in the database, read at request time.

Reading per request is what makes the GUI toggle take effect without a
container restart.
"""
from pathlib import Path

from . import db
from .models import OidcConfig

KEYS = ("oidc.enabled", "oidc.client_id", "oidc.client_secret", "oidc.issuer", "oidc.scopes")
DEFAULT_SCOPES = "openid profile email"
CALLBACK_PATH = "/auth/oidc/callback"

# issuer -> (fetched_at_monotonic, discovery document). Populated by the flow in
# routes_auth; cleared here when settings change so a new issuer is re-discovered.
_discovery_cache: dict = {}


def invalidate_discovery_cache() -> None:
    _discovery_cache.clear()


def load_oidc_config(db_path: Path) -> OidcConfig:
    values = db.get_settings(db_path, "oidc.")
    return OidcConfig(
        enabled=values.get("oidc.enabled", "false") == "true",
        client_id=values.get("oidc.client_id", "").strip(),
        client_secret=values.get("oidc.client_secret", ""),
        issuer=values.get("oidc.issuer", "").strip().rstrip("/"),
        scopes=values.get("oidc.scopes", DEFAULT_SCOPES).strip() or DEFAULT_SCOPES,
    )


def redirect_uri(request, public_base_url: str) -> str:
    """The callback URL to register with the identity provider.

    Derived from the incoming request so it reflects however the app is actually
    reached; PUBLIC_BASE_URL overrides it for reverse proxies that rewrite Host.
    """
    if public_base_url:
        return f"{public_base_url}{CALLBACK_PATH}"
    return f"{str(request.base_url).rstrip('/')}{CALLBACK_PATH}"
