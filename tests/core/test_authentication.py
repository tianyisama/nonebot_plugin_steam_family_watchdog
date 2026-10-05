import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from steam_family_watchdog_core.authentication import AuthenticationClient, LoginSession, save_auth, token_claims, validate_refresh
from steam_family_watchdog_core.errors import SteamError
from steam_family_watchdog_core.protocol import message
from steam_family_watchdog_core.steam import SteamAuth
from steam_family_watchdog_core.transport import Transport
from .helpers import A, B, jwt


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_binary_protocol_uses_fixed64_and_large_ids(self):
        value = message("GenerateAccessTokenForApp_Request", refresh_token="t", steamid=int(A), renewal_type=1)
        raw = value.SerializeToString()
        self.assertEqual(raw, b"\x0a\x01t\x11" + int(A).to_bytes(8, "little") + b"\x18\x01")
        decoded = message("GenerateAccessTokenForApp_Request").FromString(raw)
        self.assertEqual(str(decoded.steamid), A)
        # Unknown fields must remain forward-compatible.
        self.assertEqual(message("GenerateAccessTokenForApp_Request").FromString(raw + b"\xa0\x06\x01").refresh_token, "t")

    async def test_real_http_protobuf_password_code_poll_and_renewal(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = private.public_key().public_numbers()
        calls = []
        refresh, access = jwt(), jwt(refresh=False)

        async def handler(request):
            method = request.match_info["method"]
            self.assertIn("mobileClientVersion=", request.headers["Cookie"])
            if method == "GetPasswordRSAPublicKey":
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.query["origin"], "SteamMobile")
                encoded = request.query["input_protobuf_encoded"]
            else:
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.content_type, "multipart/form-data")
                encoded = (await request.post())["input_protobuf_encoded"]
            values = message(method + "_Request").FromString(base64.b64decode(encoded))
            calls.append((method, values))
            if method == "GetPasswordRSAPublicKey":
                self.assertEqual(values.account_name, "account")
                result = dict(publickey_mod=f"{public.n:x}", publickey_exp=f"{public.e:x}", timestamp=123)
            elif method == "BeginAuthSessionViaCredentials":
                self.assertEqual(private.decrypt(base64.b64decode(values.encrypted_password), padding.PKCS1v15()), b"password")
                self.assertEqual(values.website_id, "Mobile")
                self.assertEqual(values.persistence, 1)
                self.assertEqual(values.device_details.platform_type, 3)
                self.assertEqual(values.device_details.os_type, -500)
                result = dict(client_id=12345678901234567890, request_id=b"binary\x00", interval=1,
                    steamid=int(A), allowed_confirmations=[{"confirmation_type": 3}])
            elif method == "UpdateAuthSessionWithSteamGuardCode":
                self.assertEqual(values.client_id, 12345678901234567890)
                self.assertEqual(values.steamid, int(A))
                self.assertEqual(values.code, "ABCDE")
                self.assertEqual(values.code_type, 3)
                result = {}
            elif method == "PollAuthSessionStatus":
                self.assertEqual(values.request_id, b"binary\x00")
                result = dict(refresh_token=refresh, access_token=access)
            elif method == "GenerateAccessTokenForApp":
                self.assertEqual(values.steamid, int(A))
                self.assertEqual(values.renewal_type, 1)
                result = dict(access_token=access, refresh_token=refresh)
            else:
                self.fail(method)
            return web.Response(body=message(method + "_Response", **result).SerializeToString(), headers={"x-eresult": "1"})

        app = web.Application()
        app.router.add_route("*", "/IAuthenticationService/{method}/v1/", handler)
        async with TestServer(app) as server, ClientSession() as http:
            client = AuthenticationClient(Transport(http, str(server.make_url("/"))))
            session = LoginSession(client)
            await session.start_credentials("account", "password", "abcde")
            self.assertTrue(await session.poll())
            self.assertEqual(session.refresh_token, refresh)
            self.assertEqual(session.steamid, A)
            self.assertEqual((await client.generate(refresh, True)).access_token, access)
            await session.close()
        self.assertEqual([method for method, _ in calls], ["GetPasswordRSAPublicKey", "BeginAuthSessionViaCredentials",
            "UpdateAuthSessionWithSteamGuardCode", "PollAuthSessionStatus", "GenerateAccessTokenForApp"])

    async def test_qr_session_updates_client_and_challenge(self):
        class Client:
            count = 0
            async def call(self, method, **values):
                if method == "BeginAuthSessionViaQR":
                    return message(method + "_Response", client_id=1, request_id=b"r", interval=1,
                                   challenge_url="https://s.team/q/1/initial")
                self.count += 1
                if self.count == 1:
                    return message(method + "_Response", new_client_id=2, new_challenge_url="https://s.team/q/1/changed", had_remote_interaction=True)
                self.assert_client = values["client_id"]
                return message(method + "_Response", refresh_token=jwt())
        client = Client()
        session = LoginSession(client)
        self.assertEqual(await session.start_qr(), "https://s.team/q/1/initial")
        self.assertFalse(await session.poll())
        self.assertTrue(session.remote_interaction)
        self.assertEqual(session.challenge_url, "https://s.team/q/1/changed")
        self.assertTrue(await session.poll())
        self.assertEqual(client.assert_client, 2)
        session.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await session.poll()

    async def test_expired_and_wrong_platform_credentials(self):
        for token in ("invalid", jwt(expiry=time.time() - 1), jwt(audience=["web", "derive"]), jwt(refresh=False), jwt(steamid="1")):
            with self.assertRaises(SteamError) as error:
                validate_refresh(token)
            self.assertEqual(error.exception.code, "AUTH_REQUIRED")
        self.assertEqual(token_claims(jwt())["sub"], A)

    async def test_access_cache_force_and_auth_file_replacement(self):
        class Client:
            calls = []
            async def generate(self, token, renew=False):
                self.calls.append((token, renew))
                return SimpleNamespace(access_token=jwt(steamid=token_claims(token)["sub"], refresh=False), refresh_token="")
        with tempfile.TemporaryDirectory() as temp:
            client = Client()
            save_auth(Path(temp) / "auth.json", jwt())
            auth = SteamAuth(temp, client)
            first = await auth.access()
            self.assertEqual(await auth.access(), first)
            self.assertEqual(len(client.calls), 1)
            await auth.access(force=True)
            self.assertEqual(len(client.calls), 2)
            save_auth(Path(temp) / "auth.json", jwt(steamid=B))
            self.assertEqual(token_claims(await auth.access())["sub"], B)
            self.assertEqual(auth.steamid, B)
            self.assertEqual(auth.state, "authenticated")

    async def test_near_expiry_renewal_saved_but_decline_still_returns_access(self):
        renewed = jwt()
        class Client:
            renew = None
            decline = False
            async def generate(self, token, renew=False):
                self.renew = renew
                return SimpleNamespace(access_token=jwt(refresh=False), refresh_token="" if self.decline else renewed)
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "auth.json"
            save_auth(file, jwt(expiry=time.time() + 86400))
            client = Client()
            auth = SteamAuth(temp, client)
            await auth.access()
            self.assertTrue(client.renew)
            self.assertEqual(json.loads(file.read_text())["refresh_token"], renewed)
            save_auth(file, jwt(expiry=time.time() + 86400))
            client.decline = True
            self.assertTrue(await auth.access())
            self.assertTrue(client.renew)

    async def test_missing_corrupt_auth_and_mismatched_token_fail_closed(self):
        class Client:
            async def generate(self, token, renew=False):
                return SimpleNamespace(access_token=jwt(steamid=B, refresh=False), refresh_token="")
        with tempfile.TemporaryDirectory() as temp:
            auth = SteamAuth(temp, Client())
            for contents in (None, "not-json", "[]", '{"platform":"WebBrowser"}'):
                file = Path(temp) / "auth.json"
                if contents is not None:
                    file.write_text(contents)
                with self.assertRaises(SteamError):
                    await auth.access()
                self.assertEqual(auth.state, "needs_login")
            save_auth(file, jwt())
            with self.assertRaises(SteamError):
                await auth.access()
            self.assertEqual(auth.state, "needs_login")

    async def test_network_redirect_and_rate_limit_do_not_leak_credentials(self):
        async def redirect(request):
            return web.Response(status=302, headers={"Location": "/leak"})
        async def limited(request):
            return web.Response(status=429, headers={"Retry-After": "1800"})
        app = web.Application()
        app.router.add_route("*", "/IAuthenticationService/GetPasswordRSAPublicKey/v1/", redirect)
        app.router.add_route("*", "/IAuthenticationService/GenerateAccessTokenForApp/v1/", limited)
        async with TestServer(app) as server, ClientSession() as http:
            client = AuthenticationClient(Transport(http, str(server.make_url("/"))))
            with self.assertRaises(SteamError) as error:
                await client.call("GetPasswordRSAPublicKey", account_name="secret-account")
            self.assertEqual(error.exception.code, "STEAM_UNAVAILABLE")
            self.assertNotIn("secret-account", str(error.exception))
            with self.assertRaises(SteamError) as error:
                await client.generate(jwt())
            self.assertEqual(error.exception.code, "RATE_LIMIT")
            self.assertEqual(error.exception.retry_after_seconds, 1800)
