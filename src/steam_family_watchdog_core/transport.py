"""可注入的异步 HTTP 传输，所有 Steam 请求都禁用重定向。"""

import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Mapping

import aiohttp

from .errors import SteamError


def retry_after(header: str | None, now: float | None = None) -> int:
    if not header:
        return 0
    try:
        number = float(header)
        if math.isfinite(number) and number >= 0:
            return math.ceil(number)
    except ValueError:
        pass
    try:
        return max(0, math.ceil(parsedate_to_datetime(header).timestamp() - (time.time() if now is None else now)))
    except (ValueError, TypeError, OverflowError):
        return 0


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


def check_response(response: Response, authentication: bool = False) -> None:
    headers = {key.lower(): value for key, value in response.headers.items()}
    try:
        result = int(headers.get("x-eresult", "1"))
    except ValueError:
        result = -1
    retry = retry_after(headers.get("retry-after"))
    if response.status == 429 or result in (25, 84):
        raise SteamError("RATE_LIMIT", "Steam 限制了请求频率", max(900, retry), result)
    if response.status == 401 or result == 27 or (authentication and result in (5, 65, 88)):
        raise SteamError("AUTH_REQUIRED", "Steam 拒绝了登录凭据，请重新登录", eresult=result)
    if response.status == 403 or result == 15:
        raise SteamError("AUTH_REQUIRED" if authentication else "ACCESS_DENIED",
                         "Steam 拒绝访问，请检查登录凭据或家庭成员资格", eresult=result)
    if not 200 <= response.status < 300 or result != 1:
        raise SteamError("STEAM_UNAVAILABLE", f"Steam 接口暂不可用（HTTP {response.status}，EResult {result}）", retry, result)


class Transport:
    def __init__(self, session: aiohttp.ClientSession | None = None, base_url: str = "https://api.steampowered.com"):
        self.session = session
        self.owned = session is None
        self.base_url = base_url.rstrip("/")

    async def request(self, method: str, path: str, **kwargs) -> Response:
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            async with self.session.request(method, self.base_url + path, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=30), **kwargs) as response:
                return Response(response.status, response.headers, await response.read())
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            # Never expose URL/token-bearing exceptions in logs or the public API.
            raise SteamError("NETWORK_ERROR", "Steam 请求超时或网络连接失败") from error

    async def close(self) -> None:
        if self.owned and self.session is not None:
            await self.session.close()
            self.session = None
