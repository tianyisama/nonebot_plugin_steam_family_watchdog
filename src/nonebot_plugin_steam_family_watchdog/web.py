"""仅依赖 aiohttp 的管理页面；公开页面不包含管理密钥。"""

import hmac
import os
import secrets
from importlib.resources import files
from urllib.parse import urlsplit

from aiohttp import web
from pydantic import ValidationError

from steam_family_watchdog_core.http import body_json
from steam_family_watchdog_core.store import ApiError
from steam_family_watchdog_core.util import json_text

from .config import public_config

HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'",
}


class WebManager:
    def __init__(self, service, log=print):
        self.service, self.log = service, log
        self.token = ""
        self.runner = None

    def load_token(self):
        if self.service.config.steam_family_web_token:
            self.token = self.service.config.steam_family_web_token
            return
        file = self.service.root / "web-token.txt"
        try:
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(secrets.token_hex(32) + "\n")
        self.token = file.read_text(encoding="utf-8").strip()
        if len(self.token) < 32 or self.token.startswith("replace-"):
            raise ValueError("管理密钥文件无效")
        self.log(f"Steam 管理页面密钥保存在 {file}；密钥不会出现在网页或 Bot 消息中。")

    def create_app(self):
        page = files(__package__).joinpath("web.html").read_text(encoding="utf-8")

        async def handle(request):
            def send(status, data):
                return web.json_response(data, status=status, headers=HEADERS, dumps=json_text)

            if request.method == "GET" and request.path == "/":
                return web.Response(text=page, content_type="text/html", headers=HEADERS)
            actual = request.headers.get("Authorization", "").encode()
            if not self.token or not hmac.compare_digest(actual, f"Bearer {self.token}".encode()):
                return send(401, {"ok": False, "error": "unauthorized", "message": "请填写正确的管理密钥。"})
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return send(403, {"ok": False, "message": "跨站请求被拒绝。"})
            try:
                if request.method == "GET" and request.path == "/api/config":
                    return send(200, {"ok": True, "config": public_config(self.service.config)})
                if request.method == "GET" and request.path == "/api/status":
                    return send(200, {"ok": True, "status": self.service.status()})
                if request.method == "PATCH" and request.path == "/api/config":
                    config = await self.service.update_config(await body_json(request))
                    return send(200, {"ok": True, "config": config})
                if request.method == "POST" and request.path in ("/api/check", "/api/stop", "/api/resume"):
                    await body_json(request)
                    if request.path == "/api/check":
                        result = await self.service.check()
                    elif request.path == "/api/stop":
                        result = await self.service.disable()
                    else:
                        result = await self.service.enable()
                    return send(200 if result["ok"] else 409, result)
                return send(404, {"ok": False, "message": "接口不存在。"})
            except ValidationError as error:
                errors = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
                          for item in error.errors(include_input=False, include_url=False)]
                return send(422, {"ok": False, "message": "配置校验未通过。", "errors": errors})
            except ApiError as error:
                return send(error.status, {"ok": False, "message": str(error)})
            except ValueError as error:
                return send(400, {"ok": False, "message": str(error)})
            except Exception:
                return send(500, {"ok": False, "message": "本地处理失败，请检查目录权限和插件状态。"})

        app = web.Application(client_max_size=32768)
        app.router.add_route("*", "/{path:.*}", handle)
        return app

    async def start(self):
        config = self.service.config
        if not config.steam_family_web_enabled or not self.service.started:
            return
        try:
            self.load_token()
            self.runner = web.AppRunner(self.create_app(), access_log=None, shutdown_timeout=45)
            await self.runner.setup()
            await web.TCPSite(self.runner, config.steam_family_web_host, config.steam_family_web_port).start()
            self.log(f"Steam 管理页面已启动：{config.steam_family_web_host}:{config.steam_family_web_port}")
        except Exception:
            self.service.web_error = "管理页面未启动，请检查端口、目录权限或管理密钥配置。"
            self.log(self.service.web_error)
            await self.close()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
