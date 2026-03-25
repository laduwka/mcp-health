import sqlite3

import pytest

from pydantic import AnyUrl

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

import db
from auth_provider import (
    ACCESS_TOKEN_TTL,
    HealthOAuthProvider,
)

REDIRECT_URI = "https://example.com/callback"


@pytest.fixture(autouse=True)
def oauth_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    import auth_provider

    monkeypatch.setattr(auth_provider, "_conn", conn)
    monkeypatch.setattr(auth_provider, "_get_conn", lambda: conn)
    monkeypatch.setattr("config.AUTH_TOKEN", "test-secret")
    return conn


@pytest.fixture
def provider():
    return HealthOAuthProvider()


@pytest.fixture
def client_info():
    return OAuthClientInformationFull(
        client_id="test-client-123",
        redirect_uris=[AnyUrl(REDIRECT_URI)],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


@pytest.fixture
def auth_params():
    return AuthorizationParams(
        state="random-state",
        scopes=[],
        code_challenge="challenge123",
        redirect_uri=AnyUrl(REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
    )


class TestClientRegistration:
    @pytest.mark.asyncio
    async def test_register_and_get_client(self, provider, client_info):
        await provider.register_client(client_info)
        loaded = await provider.get_client("test-client-123")
        assert loaded is not None
        assert loaded.client_id == "test-client-123"
        assert str(loaded.redirect_uris[0]).rstrip("/") == REDIRECT_URI

    @pytest.mark.asyncio
    async def test_get_unknown_client(self, provider):
        result = await provider.get_client("nonexistent")
        assert result is None


class TestAuthorizationFlow:
    @pytest.mark.asyncio
    async def test_authorize_returns_login_url(
        self, provider, client_info, auth_params
    ):
        await provider.register_client(client_info)
        url = await provider.authorize(client_info, auth_params)
        assert url.startswith("/login?state=")

    @pytest.mark.asyncio
    async def test_full_flow(self, provider, client_info, auth_params):
        await provider.register_client(client_info)

        # Step 1: authorize → get login state
        login_url = await provider.authorize(client_info, auth_params)
        state_token = login_url.split("state=")[1]

        # Step 2: login callback → get auth code
        redirect_url = await provider.handle_login_callback(state_token, "test-secret")
        assert redirect_url is not None
        assert "code=mcp_ac_" in redirect_url
        assert "state=random-state" in redirect_url

        code = redirect_url.split("code=")[1].split("&")[0]

        # Step 3: load auth code
        auth_code = await provider.load_authorization_code(client_info, code)
        assert auth_code is not None
        assert auth_code.client_id == "test-client-123"
        assert auth_code.code_challenge == "challenge123"

        # Step 4: exchange code for tokens
        token = await provider.exchange_authorization_code(client_info, auth_code)
        assert token.access_token.startswith("mcp_at_")
        assert token.refresh_token.startswith("mcp_rt_")
        assert token.token_type == "Bearer"
        assert token.expires_in == ACCESS_TOKEN_TTL

        # Step 5: verify access token
        access = await provider.load_access_token(token.access_token)
        assert access is not None
        assert access.client_id == "test-client-123"

        # Code should be consumed
        assert await provider.load_authorization_code(client_info, code) is None

    @pytest.mark.asyncio
    async def test_wrong_password(self, provider, client_info, auth_params):
        await provider.register_client(client_info)
        login_url = await provider.authorize(client_info, auth_params)
        state_token = login_url.split("state=")[1]

        result = await provider.handle_login_callback(state_token, "wrong-password")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_state(self, provider):
        result = await provider.handle_login_callback("bogus-state", "test-secret")
        assert result is None


class TestTokenRefresh:
    @pytest.mark.asyncio
    async def test_refresh_flow(self, provider, client_info, auth_params):
        await provider.register_client(client_info)
        login_url = await provider.authorize(client_info, auth_params)
        state_token = login_url.split("state=")[1]
        redirect_url = await provider.handle_login_callback(state_token, "test-secret")
        code = redirect_url.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client_info, code)
        token = await provider.exchange_authorization_code(client_info, auth_code)

        # Load refresh token
        rt = await provider.load_refresh_token(client_info, token.refresh_token)
        assert rt is not None
        assert rt.client_id == "test-client-123"

        # Exchange refresh token
        new_token = await provider.exchange_refresh_token(client_info, rt, [])
        assert new_token.access_token.startswith("mcp_at_")
        assert new_token.refresh_token.startswith("mcp_rt_")
        assert new_token.access_token != token.access_token
        assert new_token.refresh_token != token.refresh_token

        # Old refresh token should be consumed
        assert (
            await provider.load_refresh_token(client_info, token.refresh_token) is None
        )


class TestTokenRevocation:
    @pytest.mark.asyncio
    async def test_revoke_access_token(self, provider, client_info, auth_params):
        await provider.register_client(client_info)
        login_url = await provider.authorize(client_info, auth_params)
        state_token = login_url.split("state=")[1]
        redirect_url = await provider.handle_login_callback(state_token, "test-secret")
        code = redirect_url.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client_info, code)
        token = await provider.exchange_authorization_code(client_info, auth_code)

        access = await provider.load_access_token(token.access_token)
        assert access is not None

        await provider.revoke_token(access)
        assert await provider.load_access_token(token.access_token) is None


class TestTokenExpiry:
    @pytest.mark.asyncio
    async def test_expired_access_token(
        self, provider, client_info, auth_params, oauth_db
    ):
        await provider.register_client(client_info)
        login_url = await provider.authorize(client_info, auth_params)
        state_token = login_url.split("state=")[1]
        redirect_url = await provider.handle_login_callback(state_token, "test-secret")
        code = redirect_url.split("code=")[1].split("&")[0]
        auth_code = await provider.load_authorization_code(client_info, code)
        token = await provider.exchange_authorization_code(client_info, auth_code)

        # Manually expire the token in DB
        oauth_db.execute(
            "UPDATE oauth_tokens SET expires_at = '2020-01-01T00:00:00' WHERE token = ?",
            (token.access_token,),
        )
        oauth_db.commit()

        assert await provider.load_access_token(token.access_token) is None


class TestLoginPage:
    def test_login_page_html(self, provider):
        html = provider.get_login_page("some-state")
        assert "some-state" in html
        assert "password" in html
        assert '<form method="POST"' in html
