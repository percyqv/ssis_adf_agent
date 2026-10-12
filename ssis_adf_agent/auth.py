"""
Bearer-token authentication for the HTTP transport.

Two mechanisms, usable together (a request is accepted if either one accepts it):

* **API keys** — ``MCP_API_KEYS`` (comma-separated). Clients send
  ``Authorization: Bearer <key>``. Simple and supported by most MCP clients
  (e.g. Copilot Studio's "API key" authentication).

* **Microsoft Entra ID (OAuth 2.0)** — ``ENTRA_TENANT_ID`` + ``ENTRA_AUDIENCE``.
  Clients obtain an access token from Entra ID for this API and send it as a
  bearer token. The signature is checked against the tenant's published keys,
  plus issuer, audience and expiry. Optionally ``ENTRA_REQUIRED_SCOPES``
  (space/comma separated) requires delegated scopes (``scp``) or app roles
  (``roles``). When ``MCP_PUBLIC_URL`` is also set, the server publishes OAuth
  Protected Resource Metadata (RFC 9728) so clients can discover Entra ID as
  the authorization server.
"""
from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field

import anyio
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [v for v in value.replace(",", " ").split() if v]


@dataclass
class AuthConfig:
    api_keys: list[str] = field(default_factory=list)
    entra_tenant_id: str | None = None
    entra_audiences: list[str] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    public_url: str | None = None  # e.g. https://ssis-adf.<region>.azurecontainerapps.io

    @classmethod
    def from_env(cls) -> AuthConfig:
        return cls(
            api_keys=[
                k.strip() for k in os.environ.get("MCP_API_KEYS", "").split(",") if k.strip()
            ],
            entra_tenant_id=os.environ.get("ENTRA_TENANT_ID") or None,
            entra_audiences=_split(os.environ.get("ENTRA_AUDIENCE")),
            required_scopes=_split(os.environ.get("ENTRA_REQUIRED_SCOPES")),
            public_url=(os.environ.get("MCP_PUBLIC_URL") or "").rstrip("/") or None,
        )

    @property
    def entra_enabled(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_audiences)

    @property
    def enabled(self) -> bool:
        return bool(self.api_keys) or self.entra_enabled

    def validate(self) -> None:
        if self.entra_tenant_id and not self.entra_audiences:
            raise ValueError("ENTRA_TENANT_ID is set but ENTRA_AUDIENCE is missing.")
        if self.entra_audiences and not self.entra_tenant_id:
            raise ValueError("ENTRA_AUDIENCE is set but ENTRA_TENANT_ID is missing.")
        weak = [k for k in self.api_keys if len(k) < 24]
        if weak:
            raise ValueError("Every MCP_API_KEYS entry must be at least 24 characters long.")


class EntraTokenValidator:
    """Validates Microsoft Entra ID access tokens (v1 and v2) for one tenant."""

    def __init__(self, tenant_id: str, audiences: list[str]) -> None:
        import jwt

        self._jwt = jwt
        self.tenant_id = tenant_id
        self.audiences = audiences
        self.issuers = [
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
            f"https://sts.windows.net/{tenant_id}/",
        ]
        self._jwks = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
            lifespan=3600,
        )

    def validate(self, token: str) -> dict | None:
        jwt = self._jwt
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=self.issuers,
                options={"require": ["exp", "iss", "aud"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            logger.info("Rejected Entra ID token: %s", exc)
            return None
        except Exception:  # network errors fetching keys, malformed tokens…
            logger.exception("Could not validate Entra ID token")
            return None


class MultiTokenVerifier:
    """``mcp.server.auth.provider.TokenVerifier`` accepting API keys and/or Entra tokens."""

    def __init__(self, config: AuthConfig, entra: EntraTokenValidator | None = None) -> None:
        self.config = config
        self._keys = [k.encode() for k in config.api_keys]
        self._entra = entra
        if self._entra is None and config.entra_enabled:
            assert config.entra_tenant_id
            self._entra = EntraTokenValidator(config.entra_tenant_id, config.entra_audiences)

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = token.encode()
        for i, key in enumerate(self._keys):
            if hmac.compare_digest(candidate, key):
                # API keys carry every required scope: they are full-access credentials.
                return AccessToken(
                    token=token,
                    client_id=f"api-key-{i + 1}",
                    scopes=list(self.config.required_scopes),
                )

        if self._entra is None or token.count(".") != 2:
            return None

        claims = await anyio.to_thread.run_sync(self._entra.validate, token)
        if claims is None:
            return None
        scopes = _split(claims.get("scp")) + list(claims.get("roles") or [])
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("appid") or "entra"),
            scopes=scopes,
            expires_at=int(claims["exp"]) if "exp" in claims else None,
            subject=claims.get("oid") or claims.get("sub"),
            claims={"iss": claims.get("iss"), "tid": claims.get("tid")},
        )
