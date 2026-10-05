"""自动认证页生命周期：共享数据锁、管理密钥和认证后恢复。"""

import asyncio
from urllib.parse import urlsplit

from aiohttp import web

from steam_family_watchdog_core.config import acquire_lock
from steam_family_watchdog_core.login_web import LoginWeb

from .access import management_token


class LoginManager:
    def __init__(self, service, controller_factory=LoginWeb):
        self.service = service
        self.controller_factory = controller_factory
        self.controller = self.runner = self.release = self.task = None
        self.error = None
        self.last_success = False
        self.success_delay = 2.0

    @property
    def active(self):
        return self.runner is not None

    async def start(self):
        if self.active:
            return True
        c = self.service.config
        urls = self.service.urls("login")
        origins = {f"http://127.0.0.1:{c.steam_family_login_port}", f"http://localhost:{c.steam_family_login_port}"}
        for url in urls.values():
            if url:
                parsed = urlsplit(url)
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
        self.last_success = False
        try:
            self.release = acquire_lock(self.service.root)
            self.controller = self.controller_factory(self.service.core_config(), c.steam_family_login_port,
                log=self.service.log, access_token=management_token(self.service), allowed_origins=origins,
                lifetime=c.steam_family_login_timeout_seconds, trusted_proxies=c.steam_family_login_trusted_proxies)
            self.runner = web.AppRunner(self.controller.create_app(), access_log=None, shutdown_timeout=45)
            await self.runner.setup()
            await web.TCPSite(self.runner, c.steam_family_login_host, c.steam_family_login_port).start()
        except Exception:
            self.error = "认证页启动失败，请检查认证端口、管理密钥或数据目录是否被其他进程占用。"
            self.service.log(self.error)
            await self._cleanup()
            return False
        self.error = None
        self.service.log(self.service.login_information())
        self.service.log(f"认证页需要管理密钥：WEB_TOKEN 或 {self.service.root / 'web-token.txt'}")
        self.task = asyncio.create_task(self._watch(), name="steam-family-login")
        return True

    async def _cleanup(self):
        try:
            if self.runner:
                await self.runner.cleanup()
        finally:
            self.runner = None
            self.controller = None
            if self.release:
                self.release()
                self.release = None

    async def _watch(self):
        current = self.controller
        expired = asyncio.create_task(current.stop_event.wait())
        authenticated = asyncio.create_task(current.authenticated_event.wait())
        success = False
        try:
            await asyncio.wait({expired, authenticated}, return_when=asyncio.FIRST_COMPLETED)
            success = current.authenticated_event.is_set()
            if success:
                await asyncio.sleep(self.success_delay)  # Keep success visible before closing.
            await self._cleanup()
            self.last_success = success
            if success:
                await self.service.authentication_finished()
            else:
                self.service.log("Steam 认证页已超时关闭；可发送 steam认证 或 steam启动 重新打开。")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.service.log("Steam 认证后恢复失败，请使用 steam检查 查看配置后重新启动。")
        finally:
            for task in (expired, authenticated):
                task.cancel()
            await asyncio.gather(expired, authenticated, return_exceptions=True)

    async def close(self):
        task = self.task
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.task = None
        await self._cleanup()
