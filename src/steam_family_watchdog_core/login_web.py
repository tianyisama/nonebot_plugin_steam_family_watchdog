"""仅本机开放的账号密码登录页面。"""

import asyncio
import hmac
import json
import re
import secrets
import time
from importlib.resources import files

from aiohttp import web

from .authentication import LoginSession, save_auth
from .http import body_json
from .store import ApiError
from .util import json_text

HEADERS = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'",
}


class LoginWeb:
    def __init__(self, config, port: int = 11453, session_factory=LoginSession, log=print):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("登录端口无效")
        self.config, self.port, self.session_factory, self.log = config, port, session_factory, log
        self.origin = f"http://127.0.0.1:{port}"
        self.csrf = secrets.token_hex(32)
        self.session = None
        self.poll_task = None
        self.shutdown_task = None
        self.expiry_task = None
        self.stop_event = asyncio.Event()
        self.busy = False
        self.attempts = 0
        self.blocked_until = 0.0
        self.login_cooldown = 0.0
        self.state = {"stage": "idle", "message": "输入 Steam 账号和密码，再使用 Steam++ 的动态验证码完成验证。"}
        self.page = files("steam_family_watchdog_core").joinpath("login.html").read_text(encoding="utf-8").replace("__CSRF_JSON__", json.dumps(self.csrf))

    def public_error(self, error: Exception) -> str:
        code = getattr(error, "eresult", None)
        if code in (25, 84) or getattr(error, "code", None) == "RATE_LIMIT":
            self.blocked_until = time.monotonic() + 15 * 60
            return "Steam 暂时限制了登录请求，请 15 分钟后重试。"
        if code == 5:
            return "账号或密码不正确，请检查后重试。"
        if code in (65, 88):
            return "验证码不正确或已过期，请使用 Steam++ 当前显示的验证码。"
        if getattr(error, "code", None) == "LOGIN_TIMEOUT":
            return "本次登录已超时，请重新输入账号和密码。"
        return "登录暂未成功，请检查账号、验证码或网络后重试。"

    async def _stop_after(self, seconds: float):
        await asyncio.sleep(seconds)
        self.stop_event.set()

    async def _poll(self, current):
        try:
            while self.session is current:
                if await current.poll():
                    try:
                        save_auth(self.config.data_dir / "auth.json", current.refresh_token)
                    except (OSError, ValueError):
                        self.state = {"stage": "error", "message": "登录成功，但凭据保存失败，请检查本机目录权限。"}
                        return
                    self.state = {"stage": "authenticated", "message": "登录成功，凭据已保存。可以关闭此页面。"}
                    self.log("Steam 登录成功；长期凭据已保存。")
                    self.shutdown_task = asyncio.create_task(self._stop_after(10))
                    return
                await asyncio.sleep(current.interval)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.session is current:
                self.state = {"stage": "error", "message": self.public_error(error)}

    async def _cancel_session(self):
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        if self.session:
            await self.session.close()
            self.session = None

    async def handle(self, request: web.Request) -> web.Response:
        def send(status, data):
            return web.json_response(data, status=status, headers=HEADERS, dumps=json_text)

        if request.headers.get("Host") != f"127.0.0.1:{self.port}":
            return send(403, {"message": "请使用本机 127.0.0.1 地址打开登录页面。"})
        if request.method == "GET" and request.path_qs == "/":
            return web.Response(text=self.page, content_type="text/html", headers=HEADERS)
        if (not hmac.compare_digest(request.headers.get("X-Login-CSRF", "").encode(), self.csrf.encode())
                or request.headers.get("Origin", self.origin) != self.origin):
            return send(403, {"message": "请求验证失败，请重新打开登录页面。"})
        if request.method == "GET" and request.path_qs == "/state":
            return send(200, self.state)
        if request.method != "POST" or request.path_qs not in ("/login", "/code"):
            return send(404, {"message": "页面不存在。"})
        if self.busy or self.state["stage"] == "authenticated":
            return send(409, self.state)
        if time.monotonic() < self.blocked_until or (request.path == "/login" and time.monotonic() < self.login_cooldown):
            return send(429, {**self.state, "message": "请稍后重试登录，避免连续提交。"})
        self.busy = True
        body = {}
        try:
            body = await body_json(request, 8192, require_content_type=False)
            if request.path == "/login":
                account, password = body.get("accountName"), body.get("password")
                if (not isinstance(account, str) or not account.strip() or len(account) > 128
                        or not isinstance(password, str) or not password or len(password) > 256):
                    return send(400, {"stage": "error", "message": "请填写正确的账号与密码。"})
                await self._cancel_session()
                current = self.session_factory()
                self.session = current
                self.attempts = 0
                self.state = {"stage": "working", "message": "正在连接 Steam…"}
                self.login_cooldown = time.monotonic() + 10
                code = body.get("code", "")
                code = code.strip().upper() if isinstance(code, str) else ""
                try:
                    await current.start_credentials(account.strip(), password, code)
                finally:
                    password = ""
                    body["password"] = ""
                if not code and any(kind in current.confirmations for kind in (2, 3)):
                    self.state = {"stage": "awaiting_code", "message": "请填写 Steam++ 当前显示的动态验证码；若账号使用邮箱验证，请填写邮箱验证码。"}
                elif any(kind in current.confirmations for kind in (4, 5)) and not code:
                    self.state = {"stage": "awaiting_confirmation", "message": "Steam 要求登录确认，请确认 Steam 提示的验证方式。"}
                else:
                    self.state = {"stage": "working", "message": "正在完成登录，请稍候…"}
                self.poll_task = asyncio.create_task(self._poll(current))
            else:
                if not self.session or self.state["stage"] not in ("awaiting_code", "awaiting_confirmation"):
                    return send(400, {"stage": "error", "message": "请先提交账号和密码。"})
                code = body.get("code")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{5,8}", code.strip()):
                    return send(400, {**self.state, "message": "请填写正确的验证码。"})
                self.attempts += 1
                if self.attempts > 3:
                    self.blocked_until = time.monotonic() + 60
                    return send(429, {**self.state, "message": "验证码尝试较多，请等待一分钟后重新开始登录。"})
                await self.session.submit_code(code.strip().upper())
                if self.state["stage"] != "authenticated":
                    self.state = {"stage": "working", "message": "验证码已提交，等待 Steam 完成登录…"}
            return send(200, self.state)
        except ApiError as error:
            return send(error.status, {"stage": "error", "message": str(error)})
        except Exception as error:
            self.state = {"stage": "awaiting_code" if request.path == "/code" and getattr(error, "eresult", None) in (65, 88) else "error",
                          "message": self.public_error(error)}
            return send(400, self.state)
        finally:
            if "password" in body:
                body["password"] = ""
            self.busy = False

    def create_app(self):
        app = web.Application(client_max_size=8192)
        app.router.add_route("*", "/{path:.*}", self.handle)

        async def startup(app):
            self.expiry_task = asyncio.create_task(self._stop_after(600))

        async def cleanup(app):
            await self._cancel_session()
            tasks = [task for task in (self.expiry_task, self.shutdown_task) if task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        app.on_startup.append(startup)
        app.on_cleanup.append(cleanup)
        return app
