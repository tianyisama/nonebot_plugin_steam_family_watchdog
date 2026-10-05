"""Steam 凭据管理和家庭库 Unified Web API。"""

import asyncio
import json
import math
import re
import time
from pathlib import Path

from .authentication import AuthenticationClient, save_auth, token_claims, validate_refresh
from .errors import SteamError
from .transport import Transport, check_response, retry_after
from .util import json_text


def auth_error(error: Exception) -> SteamError:
    if isinstance(error, SteamError):
        return error
    result = getattr(error, "eresult", None)
    if result in (5, 15, 27, 65):
        return SteamError("AUTH_REQUIRED", "Steam 登录凭据已失效或类型不匹配，请重新登录")
    if result in (25, 84):
        return SteamError("RATE_LIMIT", "Steam 限制了请求频率", 900)
    return SteamError("AUTH_NETWORK", "Steam 登录服务暂不可用，稍后重试")


class SteamAuth:
    def __init__(self, data_dir: str | Path, client: AuthenticationClient | None = None):
        self.file = Path(data_dir) / "auth.json"
        self.client = client or AuthenticationClient()
        self.signature: str | None = None
        self.state = "not_logged_in"
        self.steamid: str | None = None
        self.refresh_token = ""
        self.access_token = ""
        self._lock = asyncio.Lock()

    def load(self) -> None:
        try:
            signature = self.file.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise SteamError("AUTH_REQUIRED", "尚未登录，请运行 python -m steam_family_watchdog_core login") from error
        if signature == self.signature:
            return
        try:
            data = json.loads(signature)
            if not isinstance(data, dict):
                raise ValueError
        except ValueError as error:
            raise SteamError("AUTH_REQUIRED", "登录文件损坏，请重新登录") from error
        if data.get("platform") != "MobileApp":
            raise SteamError("AUTH_REQUIRED", "请使用本程序登录获得 MobileApp 类型的凭据")
        token = data.get("refresh_token", "")
        claims = validate_refresh(token)
        self.refresh_token = token
        self.access_token = ""
        self.steamid = claims["sub"]
        self.signature = signature
        self.state = "loaded"

    async def access(self, force: bool = False) -> str:
        async with self._lock:
            try:
                self.load()
                if not force and self.access_token:
                    expiry = token_claims(self.access_token).get("exp", 0)
                    if type(expiry) in (int, float) and expiry > time.time() + 300:
                        return self.access_token
                claims = validate_refresh(self.refresh_token)
                result = await self.client.generate(self.refresh_token, renew=claims["exp"] < time.time() + 7 * 86400)
                access_claims = token_claims(result.access_token)
                expiry = access_claims.get("exp")
                if (access_claims.get("sub") != self.steamid or type(expiry) not in (int, float)
                        or not math.isfinite(expiry) or expiry <= time.time()
                        or "derive" in access_claims.get("aud", [])):
                    raise SteamError("AUTH_REQUIRED", "Steam 未返回有效的短期登录凭据")
                if result.refresh_token:
                    renewed_claims = validate_refresh(result.refresh_token)
                    if renewed_claims["sub"] != self.steamid:
                        raise SteamError("AUTH_REQUIRED", "Steam 返回的长期凭据不属于当前账号")
                    save_auth(self.file, result.refresh_token)
                    self.refresh_token = result.refresh_token
                    self.signature = self.file.read_text(encoding="utf-8")
                self.access_token = result.access_token
                self.state = "authenticated"
                return self.access_token
            except Exception as error:
                safe = auth_error(error)
                self.state = "needs_login" if safe.code == "AUTH_REQUIRED" else "temporarily_unavailable"
                raise safe from error

    async def close(self):
        await self.client.close()


class SteamApi:
    def __init__(self, auth: SteamAuth, transport: Transport | None = None):
        self.auth = auth
        self.transport = transport or Transport()

    async def call(self, service: str, method: str, params: dict | None = None, retry_auth: bool = True) -> dict:
        token = await self.auth.access()
        response = await self.transport.request("GET", f"/{service}/{method}/v1/",
            params={"access_token": token, "input_json": json_text(params or {})},
            headers={"Accept": "application/json", "User-Agent": "SteamFamilyMonitor/1.0"})
        try:
            check_response(response)
        except SteamError as error:
            if error.code == "AUTH_REQUIRED" and retry_auth:
                await self.auth.access(force=True)
                return await self.call(service, method, params, False)
            raise
        try:
            body = json.loads(response.body)
        except (ValueError, UnicodeDecodeError) as error:
            raise SteamError("INVALID_RESPONSE", "Steam 返回了无法解析的结果，本次不更新库存") from error
        if not isinstance(body, dict) or not isinstance(body.get("response"), dict):
            raise SteamError("INVALID_RESPONSE", "Steam 返回的结果缺少 response，本次不更新库存")
        return body["response"]

    async def family(self) -> dict:
        if not self.auth.steamid:
            await self.auth.access()
        result = await self.call("IFamilyGroupsService", "GetFamilyGroupForUser", {
            "steamid": self.auth.steamid, "include_family_group_response": True,
        })
        family_id = result.get("family_groupid")
        if result.get("is_not_member_of_any_group") or not family_id or family_id == "0":
            raise SteamError("NO_FAMILY", "登录账号目前没有可用的 Steam 家庭")
        if not isinstance(family_id, str) or not re.fullmatch(r"[0-9]+", family_id):
            raise SteamError("INVALID_RESPONSE", "Steam 家庭 ID 格式无效")
        family = result.get("family_group") or {}
        raw_members = family.get("members", []) if isinstance(family, dict) else None
        if not isinstance(raw_members, list):
            raise SteamError("INVALID_RESPONSE", "Steam 家庭成员格式无效")
        members = [member.get("steamid") if isinstance(member, dict) else None for member in raw_members]
        if any(not isinstance(id_, str) or not re.fullmatch(r"[0-9]{17}", id_) for id_ in members):
            raise SteamError("INVALID_RESPONSE", "Steam 家庭成员 ID 格式无效")
        return {"id": family_id, "members": members}

    async def library(self, family_id: str, language: str) -> list:
        result = await self.call("IFamilyGroupsService", "GetSharedLibraryApps", {
            "family_groupid": family_id, "include_own": True, "include_excluded": False,
            "include_non_games": False, "language": language,
        })
        if not isinstance(result.get("apps"), list):
            raise SteamError("INVALID_RESPONSE", "Steam 未返回完整 apps 数组，本次不更新库存")
        return result["apps"]

    async def names(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        result = await self.call("IPlayerService", "GetPlayerLinkDetails", {"steamids": ids})
        accounts = result.get("accounts", [])
        if not isinstance(accounts, list):
            raise SteamError("INVALID_RESPONSE", "Steam 返回的成员昵称格式无效")
        names = {}
        for account in accounts:
            data = account.get("public_data") if isinstance(account, dict) else None
            if isinstance(data, dict) and data.get("steamid") in ids and isinstance(data.get("persona_name"), str):
                names[data["steamid"]] = data["persona_name"]
        return names

    async def close(self):
        await self.transport.close()
