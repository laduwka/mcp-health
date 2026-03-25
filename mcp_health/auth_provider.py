import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import config, db

AUTH_CODE_TTL = 600  # 10 min
ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 86400 * 30  # 30 days

_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        _conn = db.get_connection()
        db.init_db(_conn)
    return _conn


def _expiry_iso(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class HealthOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    # --- Clients ---

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = _get_conn()
        raw = db.get_oauth_client(conn, client_id)
        if raw is None:
            return None
        return OAuthClientInformationFull.model_validate_json(raw)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        conn = _get_conn()
        db.save_oauth_client(conn, client_info.client_id, client_info.model_dump_json())

    # --- Authorization ---

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        state_token = secrets.token_hex(32)
        conn = _get_conn()
        data = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "code_challenge": params.code_challenge,
            "scopes": params.scopes or [],
            "state": params.state,
            "resource": params.resource,
        }
        db.save_oauth_token(
            conn,
            token=state_token,
            token_type="login_state",
            client_id=client.client_id,
            data=json.dumps(data),
            expires_at=_expiry_iso(AUTH_CODE_TTL),
        )
        return f"/login?{urlencode({'state': state_token})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        conn = _get_conn()
        row = db.get_oauth_token(conn, authorization_code, "auth_code")
        if row is None:
            return None
        data = json.loads(row["data"])
        if data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data.get("scopes", []),
            expires_at=datetime.fromisoformat(row["expires_at"]).timestamp(),
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        conn = _get_conn()
        db.delete_oauth_token(conn, authorization_code.code)

        access = f"mcp_at_{secrets.token_hex(32)}"
        refresh = f"mcp_rt_{secrets.token_hex(32)}"

        scopes = authorization_code.scopes
        resource = authorization_code.resource

        db.save_oauth_token(
            conn,
            token=access,
            token_type="access",
            client_id=client.client_id,
            data=json.dumps({"scopes": scopes, "resource": resource}),
            expires_at=_expiry_iso(ACCESS_TOKEN_TTL),
        )
        db.save_oauth_token(
            conn,
            token=refresh,
            token_type="refresh",
            client_id=client.client_id,
            data=json.dumps({"scopes": scopes}),
            expires_at=_expiry_iso(REFRESH_TOKEN_TTL),
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    # --- Tokens ---

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        conn = _get_conn()
        row = db.get_oauth_token(conn, refresh_token, "refresh")
        if row is None:
            return None
        if row["client_id"] != client.client_id:
            return None
        data = json.loads(row["data"])
        expires_at = int(datetime.fromisoformat(row["expires_at"]).timestamp())
        if expires_at < int(time.time()):
            db.delete_oauth_token(conn, refresh_token)
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=data.get("scopes", []),
            expires_at=expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        conn = _get_conn()
        db.delete_oauth_token(conn, refresh_token.token)

        access = f"mcp_at_{secrets.token_hex(32)}"
        new_refresh = f"mcp_rt_{secrets.token_hex(32)}"
        use_scopes = scopes or refresh_token.scopes

        db.save_oauth_token(
            conn,
            token=access,
            token_type="access",
            client_id=client.client_id,
            data=json.dumps({"scopes": use_scopes}),
            expires_at=_expiry_iso(ACCESS_TOKEN_TTL),
        )
        db.save_oauth_token(
            conn,
            token=new_refresh,
            token_type="refresh",
            client_id=client.client_id,
            data=json.dumps({"scopes": use_scopes}),
            expires_at=_expiry_iso(REFRESH_TOKEN_TTL),
        )

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(use_scopes) if use_scopes else None,
            refresh_token=new_refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        conn = _get_conn()
        row = db.get_oauth_token(conn, token, "access")
        if row is None:
            return None
        expires_at = int(datetime.fromisoformat(row["expires_at"]).timestamp())
        if expires_at < int(time.time()):
            db.delete_oauth_token(conn, token)
            return None
        data = json.loads(row["data"])
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=data.get("scopes", []),
            expires_at=expires_at,
            resource=data.get("resource"),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        conn = _get_conn()
        db.delete_oauth_token(conn, token.token)

    # --- Login flow (not part of OAuthAuthorizationServerProvider) ---

    def get_login_page(self, state: str) -> str:
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>MCP Health — Login</title>
<style>
  body {{ font-family: system-ui; max-width: 400px; margin: 80px auto; padding: 0 16px; }}
  input, button {{ width: 100%; padding: 12px; margin: 8px 0; box-sizing: border-box;
    font-size: 16px; border-radius: 8px; border: 1px solid #ccc; }}
  button {{ background: #2563eb; color: white; border: none; cursor: pointer; }}
  button:hover {{ background: #1d4ed8; }}
  .error {{ color: #dc2626; margin: 8px 0; }}
</style></head>
<body>
  <h2>MCP Health</h2>
  <p>Enter your access password to authorize this client.</p>
  <form method="POST" action="/login">
    <input type="hidden" name="state" value="{state}">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Authorize</button>
  </form>
</body></html>"""

    async def handle_login_callback(self, state: str, password: str) -> str | None:
        if password != config.AUTH_TOKEN:
            return None

        conn = _get_conn()
        row = db.get_oauth_token(conn, state, "login_state")
        if row is None:
            return None

        db.delete_oauth_token(conn, state)
        data = json.loads(row["data"])

        code = f"mcp_ac_{secrets.token_hex(32)}"
        db.save_oauth_token(
            conn,
            token=code,
            token_type="auth_code",
            client_id=data["client_id"],
            data=json.dumps(data),
            expires_at=_expiry_iso(AUTH_CODE_TTL),
        )

        params = {"code": code}
        if data.get("state"):
            params["state"] = data["state"]
        return f"{data['redirect_uri']}?{urlencode(params)}"
