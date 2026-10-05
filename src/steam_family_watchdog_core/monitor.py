"""独立于 HTTP/NoneBot 的异步轮询器。"""

import asyncio
import json
import math
import random as random_module
import time

from .config import Config
from .errors import SteamError
from .util import iso_time, json_text, parse_time


def next_delay(config: Config, failures: int, error: SteamError | None = None, random=random_module.random) -> int:
    base = min(3600, config.poll_seconds * 2 ** min(failures, 10)) if failures else config.poll_seconds
    pause = 900 if error and error.code in ("AUTH_REQUIRED", "ACCESS_DENIED", "NO_FAMILY") else 0
    return max(base, error.retry_after_seconds if error else 0, pause) + math.floor(random() * (config.jitter_seconds + 1))


class Monitor:
    def __init__(self, store, api, config: Config, log=print):
        self.store, self.api, self.config, self.log = store, api, config, log
        self.family = None
        self.account = None
        self.family_expires = 0.0
        self.names_expires = 0.0
        self.failures = 0
        self.running = False
        self.stopped = False
        self.task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self.status = {
            "last_attempt_at": store.meta("last_attempt_at"),
            "last_error": json.loads(store.meta("last_error") or "null"),
            "next_check_at": store.meta("next_check_at"), "poll_seconds": config.poll_seconds,
        }

    def initial_wait(self) -> float:
        error = self.status["last_error"]
        try:
            wait = max(0.0, parse_time(self.status["next_check_at"]) - time.time()) if error else 0.0
        except (ValueError, TypeError):
            wait = 0.0
        auth_file = self.config.data_dir / "auth.json"
        if error and error.get("code") == "AUTH_REQUIRED" and auth_file.exists():
            try:
                if auth_file.stat().st_mtime > parse_time(error.get("at")):
                    wait = 0.0
            except (ValueError, TypeError, OSError):
                pass
        return wait

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.stopped = False
        self._wake.clear()
        self.task = asyncio.create_task(self._loop(), name="steam-family-monitor")

    def stop(self) -> None:
        # Wake sleeping loops, but let an in-flight scan commit before closing DB.
        self.stopped = True
        self._wake.set()

    async def wait_stopped(self) -> None:
        if self.task:
            await self.task

    async def _loop(self):
        wait = self.initial_wait()
        while not self.stopped:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except TimeoutError:
                pass
            if self.stopped:
                break
            wait = await self.tick()

    async def scan_once(self) -> dict:
        async with self._scan_lock:
            await self.api.auth.access()
            if self.account != self.api.auth.steamid:
                self.account = self.api.auth.steamid
                self.family_expires = self.names_expires = 0
            if self.family is None or time.monotonic() >= self.family_expires:
                self.family = await self.api.family()
                self.family_expires = time.monotonic() + 3600
            apps = await self.api.library(self.family["id"], self.config.language)
            if time.monotonic() >= self.names_expires:
                self.names_expires = time.monotonic() + 6 * 3600
                try:
                    self.store.save_names(await self.api.names(self.family["members"]), iso_time())
                except Exception as error:
                    if isinstance(error, SteamError) and error.code == "RATE_LIMIT":
                        raise
                    self.log("[Steam] 成员昵称暂未取得，本次仍记录游戏及成员 SteamID。")
            return self.store.scan(self.family["id"], apps, iso_time(), self.family["members"], self.config.member_aliases)

    async def tick(self) -> int:
        if self.stopped or self.running:
            return self.config.poll_seconds
        self.running = True
        failure = None
        try:
            self.status["last_attempt_at"] = iso_time()
            self.store.set_meta("last_attempt_at", self.status["last_attempt_at"])
            result = await self.scan_once()
            self.failures = 0
            self.status["last_error"] = None
            self.log(f"[Steam] {'已建立基线' if result['baseline_created'] else '检查完成'}："
                     f"{result['game_count']} 款游戏，本轮新增 {result['event_count']} 条，"
                     f"累计记录 {self.store.status()['event_count']} 条事件。")
        except Exception as error:
            failure = error if isinstance(error, SteamError) else SteamError("SCAN_FAILED", "库存验证或本地存储失败，本次不推进基线")
            self.failures += 1
            self.status["last_error"] = {"code": failure.code, "message": str(failure), "at": iso_time()}
            if failure.code in ("NO_FAMILY", "ACCESS_DENIED"):
                self.family_expires = 0
            self.log(f"[Steam] {failure}")
        finally:
            self.running = False
        seconds = next_delay(self.config, self.failures, failure)
        # Persist even when shutdown began while a scan was in flight.
        self.store.set_meta("last_error", json_text(self.status["last_error"]))
        self.status["next_check_at"] = iso_time(time.time() + seconds)
        self.store.set_meta("next_check_at", self.status["next_check_at"])
        return seconds
