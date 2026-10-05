"""Bot 内的后端生命周期、持久启用、逐目标投递与管理员告警。

APScheduler 调用 pulse()；此类不另外启动 Monitor 自带的轮询任务。
所有数据库、配置变更和投递在同一事件循环内串行执行。
"""

import asyncio
import hashlib
import json
import time
import sqlite3
from contextlib import closing
from typing import Callable

from pydantic import BaseModel, Field

from steam_family_watchdog_core.config import Config as CoreConfig, acquire_lock, atomic_json
from steam_family_watchdog_core.errors import SteamError
from steam_family_watchdog_core.monitor import Monitor
from steam_family_watchdog_core.steam import SteamApi, SteamAuth
from steam_family_watchdog_core.store import Store
from steam_family_watchdog_core.util import iso_time, parse_time

from .config import Config, EDITABLE_FIELDS, public_config
from .messages import notification, superuser_ids
from .access import address_message, page_urls, discover_public_ip
from .login import LoginManager


class AuthNotice(BaseModel):
    key: str
    sent_to: list[str] = Field(default_factory=list)


class State(BaseModel):
    activated: bool = False
    enabled: bool = False
    bot_id: str | None = None
    auth_notice: AuthNotice | None = None
    pending_activation: bool = False
    credential_retry_at: float = 0


class Service:
    def __init__(self, config: Config, bots: Callable, superusers: Callable, log=print,
                 store_factory=Store, auth_factory=SteamAuth, api_factory=SteamApi,
                 login_manager_factory=LoginManager, public_ip_provider=discover_public_ip):
        self.base_config = config
        self.config = config
        self.bots, self.superusers, self.log = bots, superusers, log
        self.store_factory, self.auth_factory, self.api_factory = store_factory, auth_factory, api_factory
        self.root = config.steam_family_data_dir
        self.config_file = self.root / "plugin-config.json"
        self.state_file = self.root / "plugin-state.json"
        self.state = State()
        self.store = self.auth = self.api = self.monitor = None
        self.backend_release = self.plugin_release = None
        self.started = False
        self.closing = False
        self.startup_error: str | None = None
        self.web_error: str | None = None
        self.next_scan = 0.0
        self.target_retries: dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.login_manager = login_manager_factory(self)
        self.public_ip_provider = public_ip_provider
        self.public_ip = config.steam_family_public_ip
        self.web_running = False
        self.credential_state = "not_logged_in"
        self.credential_signature = ""
        self.credential_checked_until = 0.0
        self.last_monitor_status = {}

    async def discover_addresses(self):
        if self.public_ip or not self.config.steam_family_auto_public_ip:
            return
        if self.config.steam_family_login_public_url and self.config.steam_family_web_public_url:
            return
        try:
            self.public_ip = await self.public_ip_provider()
        except Exception:
            self.log("Steam 家庭库：公网 IP 获取失败，本地地址仍可使用；可设置 PUBLIC_IP 或 PUBLIC_URL。")

    def urls(self, kind: str) -> dict[str, str]:
        c = self.config
        host, port, override = (c.steam_family_login_host, c.steam_family_login_port, c.steam_family_login_public_url) if kind == "login" else (c.steam_family_web_host, c.steam_family_web_port, c.steam_family_web_public_url)
        public = override or (page_urls(self.public_ip, port)["local"] if self.public_ip and kind != "login" else "")
        return page_urls(host, port, public)

    def _credential_fingerprint(self):
        try:
            contents = (self.root / "auth.json").read_bytes()
        except OSError:
            contents = b"missing-or-unreadable"
        return hashlib.sha256(contents).hexdigest()

    async def _probe_credentials(self):
        auth = self.auth_factory(self.root)
        try:
            auth.load()
            signature = self._credential_fingerprint()
            if signature == self.credential_signature and time.time() < self.credential_checked_until:
                return self.credential_state != "needs_login"
            saved_pause = self._saved_rate_pause()
            paused = max(self.state.credential_retry_at, saved_pause) > time.time() or (self.monitor and self.monitor.status["last_error"]
                and self.monitor.status["last_error"]["code"] == "RATE_LIMIT" and self.monitor.initial_wait() > 0)
            if paused:
                self.credential_state = "temporarily_unavailable"
                return True  # Locally valid, online validation deferred until the pause ends.
            await auth.access()
            self.credential_state = "authenticated"
            self.credential_signature = self._credential_fingerprint()
            self.credential_checked_until = time.time() + 300
            if self.state.credential_retry_at or self.state.auth_notice is not None:
                self.state.credential_retry_at = 0
                self.state.auth_notice = None
                self._save_state()
            return True
        except SteamError as error:
            self.credential_signature = self._credential_fingerprint()
            if error.code == "AUTH_REQUIRED":
                self.credential_state = "needs_login"
                self.credential_checked_until = time.time() + 300
                return False
            self.credential_state = "temporarily_unavailable"
            self.state.credential_retry_at = time.time() + max(60, error.retry_after_seconds)
            self._save_state()
            self.log("Steam 凭据在线检查暂不可用，保留凭据并遵守重试等待。")
            return True
        finally:
            await auth.close()

    def _saved_rate_pause(self):
        file = self.root / "monitor.sqlite3"
        if not file.exists():
            return 0.0
        try:
            with closing(sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                values = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('last_error','next_check_at')"))
            error = json.loads(values.get("last_error", "null"))
            return parse_time(values.get("next_check_at")) if error and error["code"] == "RATE_LIMIT" else 0.0
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            return 0.0

    async def _require_login(self):
        self.credential_state = "needs_login"
        self.credential_signature = self._credential_fingerprint()
        self.credential_checked_until = time.time() + 300
        await self._close_backend()
        await self.login_manager.start()
        self._record_auth_notice()

    def login_information(self):
        if self.login_manager.active:
            urls = self.urls("login")
            public = urls["public"] or "未配置 HTTPS 地址；公网 HTTP 禁止认证"
            return f"Steam 认证页\n本地地址：{urls['local']}\n公网地址：{public}\n请使用 WEB_TOKEN 或数据目录 web-token.txt 中的管理密钥。"
        return self.login_manager.error or "认证页未运行，可发送 steam认证 或 steam启动 检查并重新打开。"

    async def request_login(self, bot=None):
        async with self.lock:
            if not self.started or self.closing:
                return {"ok": False, "message": "插件尚未启动。"}
            if self.login_manager.active:
                return {"ok": True, "message": self.login_information()}
            if await self._probe_credentials():
                return {"ok": True, "message": "当前凭据本地有效；在线结果和重试状态可通过 steam状态 查看。"}
            await self._require_login()
            await self._notify_superusers(self.select_bot(bot))
            return {"ok": self.login_manager.active, "message": self.login_information()}

    async def authentication_finished(self):
        async with self.lock:
            if self.closing:
                return
            self.credential_state = "authenticated"
            self.credential_signature = self._credential_fingerprint()
            self.credential_checked_until = time.time() + 300
            self.state.auth_notice = None
            should_resume = self.state.enabled or self.state.pending_activation
            check = await self.check()
            if should_resume and check["ok"]:
                self._open_backend()
                self.state.activated = self.state.enabled = True
                self.state.pending_activation = False
                self.state.bot_id = check["bot_id"]
                self.log("Steam 认证完成，监控已自动恢复。")
            else:
                self.log("Steam 认证完成；请使用 steam检查 确认配置后发送 steam启动。")
            self._save_state()

    def core_config(self) -> CoreConfig:
        c = self.config
        return CoreConfig(data_dir=self.root, poll_seconds=c.steam_family_poll_seconds,
            jitter_seconds=c.steam_family_jitter_seconds, language=c.steam_family_language,
            missing_confirmations=c.steam_family_missing_confirmations, member_aliases=c.steam_family_member_aliases)

    def _save_state(self):
        atomic_json(self.state_file, self.state.model_dump(mode="json"))

    def _read_files(self):
        if self.config_file.exists():
            patch = json.loads(self.config_file.read_text(encoding="utf-8"))
            if not isinstance(patch, dict) or set(patch) - EDITABLE_FIELDS:
                raise ValueError("plugin-config.json 包含不可修改的字段")
            self.config = Config.model_validate({**self.base_config.model_dump(), **patch})
        if self.state_file.exists():
            self.state = State.model_validate_json(self.state_file.read_text(encoding="utf-8"))
            if self.state.enabled and not self.state.activated:
                raise ValueError("plugin-state.json 启用状态无效")

    async def startup(self):
        async with self.lock:
            if self.started:
                return
            try:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                owner_dir = self.root / ".plugin"
                owner_dir.mkdir(exist_ok=True, mode=0o700)
                self.plugin_release = acquire_lock(owner_dir)
                self._read_files()
                self.started = True
                if self.state.enabled:
                    self._open_backend()
                if not await self._probe_credentials():
                    await self._require_login()
            except Exception:
                self.startup_error = "后端初始化失败，请检查运行配置、数据库权限或是否有其他实例占用数据目录。"
                self.log(self.startup_error)
                # Keep the management page and commands usable for diagnostics.
                self.started = self.plugin_release is not None
            if not superuser_ids(self.superusers()):
                self.log("Steam 家庭库：未配置 OneBot v11 SUPERUSERS，无法启用或发送凭据失效告警。")

    def _open_backend(self):
        if self.monitor:
            return
        release = acquire_lock(self.root)
        store = None
        try:
            store = self.store_factory(self.root / "monitor.sqlite3", self.config.steam_family_missing_confirmations)
            auth = self.auth_factory(self.root)
            api = self.api_factory(auth)
            monitor = Monitor(store, api, self.core_config(), self.log)
        except Exception:
            if store:
                store.close()
            release()
            raise
        self.backend_release = release
        self.store, self.auth, self.api, self.monitor = store, auth, api, monitor
        self.next_scan = time.time() + monitor.initial_wait()
        self.next_scan = max(self.next_scan, self.state.credential_retry_at)

    async def _close_backend(self):
        if self.monitor:
            self.last_monitor_status = {**self.store.status(), **self.monitor.status}
            self.monitor.stop()
        try:
            if self.api:
                await self.api.close()
        finally:
            try:
                if self.auth:
                    await self.auth.close()
            finally:
                if self.store:
                    self.store.close()
                if self.backend_release:
                    self.backend_release()
                self.store = self.auth = self.api = self.monitor = None
                self.backend_release = None

    async def shutdown(self):
        self.closing = True
        await self.login_manager.close()
        async with self.lock:
            try:
                await self._close_backend()
            finally:
                if self.plugin_release:
                    self.plugin_release()
                    self.plugin_release = None
                self.started = False

    def select_bot(self, preferred=None):
        bots = self.bots()
        desired = self.config.steam_family_bot_id or self.state.bot_id
        if preferred is not None and (not desired or str(preferred.self_id) == desired):
            return preferred
        for id_, bot in sorted(bots.items()):
            if bot.adapter.get_name() == "OneBot V11" and (not desired or str(id_) == desired):
                return bot
        return None

    def targets(self):
        return [("group", id_) for id_ in self.config.steam_family_push_groups] + [("private", id_) for id_ in self.config.steam_family_push_users]

    @staticmethod
    def client_id(kind: str, id_: str) -> str:
        return f"nonebot:{kind}:{id_}"

    async def check(self, bot=None) -> dict:
        """无网络、无扫描的配置/凭据检查，不绕过 Steam 限流。"""
        errors, warnings = [], []
        if self.startup_error:
            errors.append(self.startup_error)
        if not self.targets():
            errors.append("推送群聊和个人列表均为空。")
        if not superuser_ids(self.superusers()):
            errors.append("未配置有效的 OneBot v11 SUPERUSERS。")
        selected = self.select_bot(bot)
        if not selected:
            errors.append("配置的 OneBot v11 Bot 尚未连接，或 Bot ID 与当前 Bot 不符。")
        auth = self.auth_factory(self.root)
        auth_valid = False
        try:
            auth.load()
            auth_valid = True
            if self.credential_state == "needs_login" and self._credential_fingerprint() == self.credential_signature:
                auth_valid = False
                errors.append("Steam 凭据已失效，需要完成认证页登录。")
        except SteamError as error:
            errors.append(str(error))
        except Exception:
            errors.append("无法读取 auth.json，请检查数据目录权限。")
        finally:
            await auth.close()
        if self.web_error:
            warnings.append(self.web_error)
        if self.login_manager.error:
            warnings.append(self.login_manager.error)
        if not self.state.activated:
            warnings.append("首次使用需要超级用户发送 steam启动。")
        return {"ok": not errors, "errors": errors, "warnings": warnings, "auth_valid": auth_valid,
                "bot_id": str(selected.self_id) if selected else None,
                "groups": len(self.config.steam_family_push_groups), "users": len(self.config.steam_family_push_users)}

    async def enable(self, bot=None, from_command=False) -> dict:
        async with self.lock:
            if not self.started or self.closing:
                return {"ok": False, "errors": ["插件后端尚未完成启动。"]}
            if self.startup_error and self.monitor is None:
                try:
                    self._read_files()
                    self.startup_error = None
                except Exception:
                    pass
            if not self.state.activated and not self.state.pending_activation and not from_command:
                return {"ok": False, "errors": ["首次启用必须由超级用户发送 steam启动。"]}
            if not self.login_manager.active:
                if not await self._probe_credentials():
                    await self._require_login()
            check = await self.check(bot)
            if not check["ok"]:
                if not check["auth_valid"]:
                    if from_command and self.targets() and superuser_ids(self.superusers()) and self.select_bot(bot) and not self.startup_error:
                        self.state.pending_activation = True
                        self.state.bot_id = str(self.select_bot(bot).self_id)
                        self._save_state()
                    if not self.login_manager.active:
                        await self._require_login()
                    self._record_auth_notice()
                    await self._notify_superusers(self.select_bot(bot))
                    check["login_pending"] = True
                    check["warnings"].append(self.login_information())
                return check
            if self.login_manager.active:
                if from_command:
                    self.state.pending_activation = True
                    self.state.bot_id = check["bot_id"]
                    self._save_state()
                return {"ok": False, "errors": ["正在完成认证，请等待认证页关闭后自动恢复。"], "warnings": [self.login_information()]}
            if not self.monitor:
                try:
                    self._open_backend()
                except Exception:
                    return {"ok": False, "errors": ["数据目录正在使用或数据库无法打开，请先停止独立监控 / 登录程序。"]}
            previous = self.state.model_copy(deep=True)
            self.state.activated = True
            self.state.enabled = True
            self.state.pending_activation = False
            self.state.bot_id = check["bot_id"]
            try:
                self._save_state()
            except Exception:
                self.state = previous
                if not previous.enabled:
                    await self._close_backend()
                return {"ok": False, "errors": ["启用状态保存失败，请检查目录权限。"]}
            return {**check, "enabled": True}

    async def disable(self) -> dict:
        async with self.lock:
            if not self.started or self.closing:
                raise ValueError("插件尚未启动或正在关闭")
            previous = self.state.enabled
            self.state.enabled = False
            self.state.pending_activation = False
            try:
                self._save_state()
            except Exception:
                self.state.enabled = previous
                raise
            await self._close_backend()
            await self.login_manager.close()
            self.target_retries.clear()
            return {"ok": True, "enabled": False}

    def _sync_targets(self):
        if self.store and self.store.ready():
            for kind, id_ in self.targets():
                self.store.register(self.client_id(kind, id_), "beginning" if self.config.steam_family_replay_history else "latest")

    def _record_auth_notice(self):
        try:
            contents = (self.root / "auth.json").read_bytes()
        except OSError:
            contents = b"missing-or-unreadable"
        key = hashlib.sha256(contents).hexdigest()
        if self.state.auth_notice is None or self.state.auth_notice.key != key:
            self.state.auth_notice = AuthNotice(key=key)
            self._save_state()

    async def _notify_superusers(self, bot):
        notice = self.state.auth_notice
        if not notice or not bot:
            return
        message = "Steam 家庭库监控：凭据缺失或失效，需要登录。\n" + self.login_information()
        for id_ in superuser_ids(self.superusers(), bot.adapter.get_name()):
            if id_ in notice.sent_to:
                continue
            key = f"superuser:{id_}"
            if self.target_retries.get(key, 0) > time.time():
                continue
            try:
                await asyncio.wait_for(bot.send_private_msg(user_id=int(id_), message=message, auto_escape=True), timeout=30)
            except Exception:
                self.target_retries[key] = time.time() + self.config.steam_family_push_retry_seconds
                self.log(f"Steam 家庭库：超级用户 {id_} 告警发送失败，将重试。")
            else:
                notice.sent_to.append(id_)
                self._save_state()

    async def _push(self, bot):
        if not bot or not self.store or not self.store.ready():
            return
        for kind, id_ in self.targets():
            clientid = self.client_id(kind, id_)
            if self.target_retries.get(clientid, 0) > time.time():
                continue
            for _ in range(self.config.steam_family_max_push_per_tick):
                batch = self.store.changes(clientid, 1)
                if not batch["count"]:
                    break
                message = notification(batch["new_games"], self.config.steam_family_send_images)
                try:
                    if kind == "group":
                        await asyncio.wait_for(bot.send_group_msg(group_id=int(id_), message=message), timeout=30)
                    else:
                        await asyncio.wait_for(bot.send_private_msg(user_id=int(id_), message=message), timeout=30)
                except Exception:
                    self.target_retries[clientid] = time.time() + self.config.steam_family_push_retry_seconds
                    self.log(f"Steam 家庭库：{kind} 目标 {id_} 推送失败，保留未确认批次。")
                    break
                self.store.ack(clientid, batch["delivery_id"])

    async def pulse(self, preferred_bot=None):
        if not self.started or self.closing or self.lock.locked():
            return
        async with self.lock:
            bot = self.select_bot(preferred_bot)
            if self.login_manager.active or self.credential_state == "needs_login":
                if not self.login_manager.active and self._credential_fingerprint() != self.credential_signature:
                    if await self._probe_credentials():
                        self.credential_state = "authenticated"
                    else:
                        await self._require_login()
                if self.login_manager.active or self.credential_state == "needs_login":
                    await self._notify_superusers(bot)
                    return
            if self.state.pending_activation and not self.state.enabled:
                check = await self.check(bot)
                if check["ok"]:
                    self._open_backend()
                    self.state.activated = self.state.enabled = True
                    self.state.pending_activation = False
                    self.state.bot_id = check["bot_id"]
                    self._save_state()
            if not self.state.enabled or self.startup_error:
                await self._notify_superusers(bot)
                return
            self._open_backend()
            self._sync_targets()
            if self.monitor.status["last_error"] and self.monitor.status["last_error"]["code"] == "AUTH_REQUIRED":
                self.next_scan = min(self.next_scan, time.time() + self.monitor.initial_wait())
            if time.time() >= self.next_scan:
                seconds = await self.monitor.tick()
                self.next_scan = time.time() + seconds
                error = self.monitor.status["last_error"]
                if error and error["code"] == "AUTH_REQUIRED":
                    await self._require_login()
                    await self._notify_superusers(bot)
                    return
                elif not error and self.state.auth_notice is not None:
                    self.state.auth_notice = None
                    self._save_state()
                self._sync_targets()
            await self._notify_superusers(bot)
            await self._push(bot)

    async def update_config(self, patch: dict) -> dict:
        async with self.lock:
            if not self.started or self.closing:
                raise ValueError("插件尚未启动")
            if set(patch) - EDITABLE_FIELDS:
                raise ValueError("包含不可修改的配置项")
            candidate = Config.model_validate({**self.config.model_dump(), **patch})
            atomic_json(self.config_file, public_config(candidate))
            self.config = candidate
            if self.monitor:
                self.monitor.config = self.core_config()
                self.monitor.status["poll_seconds"] = candidate.steam_family_poll_seconds
                self.store.missing_confirmations = candidate.steam_family_missing_confirmations
                # A config save must never reset a persisted failure/rate-limit pause.
                if self.monitor.status["last_error"] is None:
                    self.next_scan = max(time.time(), parse_time(self.monitor.status["last_attempt_at"]) + candidate.steam_family_poll_seconds)
                    self.monitor.status["next_check_at"] = iso_time(self.next_scan)
                    self.store.set_meta("next_check_at", self.monitor.status["next_check_at"])
                self._sync_targets()
            return public_config(self.config)

    def status(self) -> dict:
        return {
            "activated": self.state.activated, "enabled": self.state.enabled,
            "backend_running": self.monitor is not None, "bot_connected": self.select_bot() is not None,
            "bot_id": self.config.steam_family_bot_id or self.state.bot_id,
            "startup_error": self.startup_error, "web_error": self.web_error,
            "groups": len(self.config.steam_family_push_groups), "users": len(self.config.steam_family_push_users),
            "auth_state": self.auth.state if self.auth else self.credential_state,
            "pending_activation": self.state.pending_activation,
            "login_running": self.login_manager.active, "login_urls": self.urls("login"),
            "login_error": self.login_manager.error, "web_urls": self.urls("web"),
            "auth_notice_active": bool(self.state.auth_notice),
            "auth_notice_pending": bool(self.state.auth_notice and
                set(superuser_ids(self.superusers())) - set(self.state.auth_notice.sent_to)),
            **(self.store.status() if self.store else self.last_monitor_status),
            **(self.monitor.status if self.monitor else {}),
        }
