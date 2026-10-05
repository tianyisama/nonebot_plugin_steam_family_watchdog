"""管理/认证页共用的密钥与可访问地址。"""

import os
import secrets
from ipaddress import ip_address

import aiohttp


def management_token(service) -> str:
    if service.config.steam_family_web_token:
        return service.config.steam_family_web_token
    file = service.root / "web-token.txt"
    try:
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    token = file.read_text(encoding="utf-8").strip()
    if len(token) < 32 or token.startswith("replace-"):
        raise ValueError("管理密钥文件无效")
    return token


def page_urls(host: str, port: int, public_url: str = "") -> dict[str, str]:
    local_host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    return {"local": f"http://{local_host}:{port}/", "public": public_url}


def address_message(title: str, urls: dict[str, str]) -> str:
    return f"{title}\n本地地址：{urls['local']}\n公网地址：{urls['public'] or '暂未获取，可配置 PUBLIC_IP 或对应的 PUBLIC_URL'}"


async def discover_public_ip() -> str:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.get("https://api.ipify.org?format=json", allow_redirects=False) as response:
            if response.status != 200 or response.content_length and response.content_length > 256:
                raise ValueError("公网 IP 响应无效")
            data = await response.content.read(257)
            if len(data) > 256:
                raise ValueError("公网 IP 响应过大")
            import json
            value = ip_address(json.loads(data)["ip"])
            if not value.is_global:
                raise ValueError("返回的地址不是公网 IP")
            return str(value)
