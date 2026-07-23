"""OIDC configuration held in the database, read at request time.

Reading per request is what makes the GUI toggle take effect without a
container restart. Only authlib's JOSE primitives are used for the security-
critical part (JWKS handling + ID-token signature and claim validation); the
two HTTP calls are plain httpx against config read from the DB.
"""
import base64
import hashlib
import time
from pathlib import Path

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from authlib.oidc.core import CodeIDToken

from . import db
from .models import OidcConfig

DISCOVERY_TTL = 3600
_ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]


class OidcError(Exception):
    """A user-facing OIDC failure; the message is safe to show."""

KEYS = ("oidc.enabled", "oidc.client_id", "oidc.client_secret", "oidc.issuer", "oidc.scopes")
DEFAULT_SCOPES = "openid profile email"
CALLBACK_PATH = "/auth/oidc/callback"

# issuer -> (fetched_at_monotonic, discovery document). Populated by the flow in
# routes_auth; cleared here when settings change so a new issuer is re-discovered.
_discovery_cache: dict = {}


def invalidate_discovery_cache() -> None:
    _discovery_cache.clear()


def create_s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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


async def discover(issuer: str, client: httpx.AsyncClient) -> dict:
    cached = _discovery_cache.get(issuer)
    if cached and time.monotonic() - cached[0] < DISCOVERY_TTL:
        return cached[1]
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        meta = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"Could not read the provider's discovery document at {url}.") from exc
    if meta.get("issuer", "").rstrip("/") != issuer:
        # The issuer in the document must match what we asked for, or the rest of
        # the flow is trusting a document we did not authenticate.
        raise OidcError("The provider's issuer does not match the configured URL.")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not meta.get(key):
            raise OidcError(f"The provider's discovery document is missing '{key}'.")
    _discovery_cache[issuer] = (time.monotonic(), meta)
    return meta


async def exchange_code(
    cfg: OidcConfig, meta: dict, code: str, redirect_uri_value: str, code_verifier: str,
    client: httpx.AsyncClient,
) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri_value,
        "client_id": cfg.client_id,
        "code_verifier": code_verifier,
    }
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    try:
        resp = await client.post(meta["token_endpoint"], data=data)
    except httpx.HTTPError as exc:
        raise OidcError("Could not reach the provider's token endpoint.") from exc
    if resp.status_code != 200:
        raise OidcError("The provider rejected the login (token exchange failed).")
    tokens = resp.json()
    if "id_token" not in tokens:
        raise OidcError("The provider did not return an ID token.")
    return tokens


async def _load_jwks(jwks_uri: str, client: httpx.AsyncClient):
    resp = await client.get(jwks_uri)
    resp.raise_for_status()
    return JsonWebKey.import_key_set(resp.json())


async def validate_id_token(
    id_token: str, cfg: OidcConfig, meta: dict, nonce: str, client: httpx.AsyncClient
) -> dict:
    """Verify the ID token's signature and claims. This is the security core."""
    jwt = JsonWebToken(_ID_TOKEN_ALGS)
    claims_options = {
        "iss": {"essential": True, "value": meta["issuer"]},
        "aud": {"essential": True, "value": cfg.client_id},
    }
    try:
        keyset = await _load_jwks(meta["jwks_uri"], client)
        claims = jwt.decode(
            id_token, keyset, claims_cls=CodeIDToken, claims_options=claims_options,
            claims_params={"nonce": nonce, "client_id": cfg.client_id},
        )
        claims.validate(leeway=120)  # exp/iat/nonce/azp per the OIDC spec
    except (JoseError, httpx.HTTPError, ValueError) as exc:
        raise OidcError("The provider's ID token failed validation.") from exc
    return dict(claims)


def claim_username(claims: dict) -> str:
    for key in ("preferred_username", "email", "sub"):
        value = claims.get(key)
        if value and str(value).strip():
            return str(value).strip().lower()
    raise OidcError("The provider did not supply a usable username claim.")
