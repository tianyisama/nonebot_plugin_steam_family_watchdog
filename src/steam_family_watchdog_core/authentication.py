"""原生 Python Steam MobileApp 认证：密码/RSA、验证码、二维码及续期。"""

import asyncio
import base64
import json
import math
import re
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from google.protobuf.message import DecodeError

from .config import atomic_json
from .errors import SteamError
from .protocol import message
from .transport import Transport, check_response
from .util import iso_time

MOBILE_HEADERS = {
    "Accept": "application/json, text/plain, */*", "User-Agent": "okhttp/4.9.2",
    "Cookie": "mobileClient=android; mobileClientVersion=777777 3.10.3",
    "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty",
}
DEVICE = {"device_friendly_name": "Steam Family Watchdog", "platform_type": 3, "os_type": -500, "gaming_device_type": 528}


def token_claims(token: str) -> dict:
    """只读取 JWT 元数据；实际凭据有效性仍由 Steam 服务端验证。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError, AttributeError) as error:
        raise SteamError("AUTH_REQUIRED", "登录凭据格式无效，请重新登录") from error


def validate_refresh(token: str) -> dict:
    claims = token_claims(token)
    audience = claims.get("aud")
    expiry = claims.get("exp")
    if (not isinstance(audience, list) or "mobile" not in audience or "derive" not in audience
            or not isinstance(claims.get("sub"), str) or not re.fullmatch(r"[0-9]{17}", claims["sub"])):
        raise SteamError("AUTH_REQUIRED", "请使用本程序登录获得 MobileApp 类型的长期凭据")
    if (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time()):
        raise SteamError("AUTH_REQUIRED", "长期登录凭据已过期，请重新登录")
    return claims


def save_auth(file: Path, token: str) -> None:
    claims = validate_refresh(token)
    atomic_json(file, {"platform": "MobileApp", "steamid": claims["sub"], "refresh_token": token, "saved_at": iso_time()})


class AuthenticationClient:
    def __init__(self, transport: Transport | None = None):
        self.transport = transport or Transport()

    async def call(self, method: str, **values):
        encoded = base64.b64encode(message(method + "_Request", **values).SerializeToString()).decode("ascii")
        if method == "GetPasswordRSAPublicKey":
            response = await self.transport.request("GET", f"/IAuthenticationService/{method}/v1/",
                params={"input_protobuf_encoded": encoded, "origin": "SteamMobile"}, headers=MOBILE_HEADERS)
        else:
            response = await self._post(method, encoded)
        check_response(response, authentication=True)
        try:
            return message(method + "_Response").FromString(response.body)
        except DecodeError as error:
            raise SteamError("INVALID_RESPONSE", "Steam 认证服务返回了无法解析的结果") from error

    async def _post(self, method: str, encoded: str):
        # Matches steam-session's multipart form transport, not input_json.
        import aiohttp
        form = aiohttp.FormData()
        form.add_field("input_protobuf_encoded", encoded, content_type="text/plain")
        return await self.transport.request("POST", f"/IAuthenticationService/{method}/v1/", data=form, headers=MOBILE_HEADERS)

    async def generate(self, token: str, renew: bool = False):
        claims = validate_refresh(token)
        return await self.call("GenerateAccessTokenForApp", refresh_token=token, steamid=int(claims["sub"]), renewal_type=int(renew))

    async def close(self):
        await self.transport.close()


class LoginSession:
    """单次登录会话；不保存密码，poll() 可由任何宿主异步调用。"""

    def __init__(self, client: AuthenticationClient | None = None, timeout: float = 180):
        self.client = client or AuthenticationClient()
        self.client_id = 0
        self.request_id = b""
        self.steamid: str | None = None
        self.interval = 5.0
        self.confirmations: list[int] = []
        self.challenge_url = ""
        self.refresh_token = ""
        self.access_token = ""
        self.remote_interaction = False
        self.timeout = timeout
        self.deadline = 0.0
        self.cancelled = False

    def _start(self, response):
        if not response.client_id or not response.request_id:
            raise SteamError("INVALID_RESPONSE", "Steam 未返回完整的登录会话")
        self.client_id = response.client_id
        self.request_id = response.request_id
        self.interval = max(1.0, response.interval or 5.0)
        self.confirmations = [action.confirmation_type for action in response.allowed_confirmations]
        self.deadline = time.monotonic() + self.timeout

    async def start_credentials(self, account_name: str, password: str, code: str = "") -> None:
        rsa_info = await self.client.call("GetPasswordRSAPublicKey", account_name=account_name)
        try:
            key = rsa.RSAPublicNumbers(int(rsa_info.publickey_exp, 16), int(rsa_info.publickey_mod, 16)).public_key()
            encrypted = base64.b64encode(key.encrypt(password.encode("utf-8"), padding.PKCS1v15())).decode("ascii")
        except ValueError as error:
            raise SteamError("INVALID_RESPONSE", "Steam 密码加密参数无效") from error
        finally:
            password = ""
        response = await self.client.call("BeginAuthSessionViaCredentials", account_name=account_name,
            encrypted_password=encrypted, encryption_timestamp=rsa_info.timestamp, remember_login=True,
            persistence=1, website_id="Mobile", device_details=DEVICE)
        self._start(response)
        self.steamid = str(response.steamid)
        if not re.fullmatch(r"[0-9]{17}", self.steamid):
            raise SteamError("INVALID_RESPONSE", "Steam 登录会话缺少有效的 SteamID")
        if code and any(kind in self.confirmations for kind in (2, 3)):
            await self.submit_code(code)

    async def start_qr(self) -> str:
        response = await self.client.call("BeginAuthSessionViaQR", device_details=DEVICE)
        self._start(response)
        self.challenge_url = response.challenge_url
        if not self.challenge_url.startswith("https://s.team/q/"):
            raise SteamError("INVALID_RESPONSE", "Steam 未返回有效的二维码地址")
        return self.challenge_url

    async def submit_code(self, code: str) -> None:
        if not self.client_id or not self.steamid or self.cancelled:
            raise SteamError("AUTH_REQUIRED", "请先开始账号密码登录")
        kind = 3 if 3 in self.confirmations else 2 if 2 in self.confirmations else 0
        if not kind:
            raise SteamError("AUTH_REQUIRED", "本次登录需要在 Steam 提示的设备或邮箱中确认")
        await self.client.call("UpdateAuthSessionWithSteamGuardCode", client_id=self.client_id,
            steamid=int(self.steamid), code=code.strip().upper(), code_type=kind)

    async def poll(self) -> bool:
        if self.cancelled:
            raise asyncio.CancelledError
        if not self.client_id:
            raise SteamError("AUTH_REQUIRED", "登录会话尚未开始")
        if time.monotonic() >= self.deadline:
            raise SteamError("LOGIN_TIMEOUT", "本次登录已超时，请重新登录")
        result = await self.client.call("PollAuthSessionStatus", client_id=self.client_id, request_id=self.request_id)
        if result.new_client_id:
            self.client_id = result.new_client_id
        if result.new_challenge_url:
            self.challenge_url = result.new_challenge_url
        self.remote_interaction = self.remote_interaction or result.had_remote_interaction
        if result.refresh_token:
            claims = validate_refresh(result.refresh_token)
            if self.steamid and self.steamid != claims["sub"]:
                raise SteamError("INVALID_RESPONSE", "Steam 返回了其他账号的登录凭据")
            self.steamid = claims["sub"]
            self.refresh_token = result.refresh_token
            self.access_token = result.access_token
            return True
        return False

    def cancel(self):
        self.cancelled = True

    async def close(self):
        self.cancel()
        await self.client.close()
