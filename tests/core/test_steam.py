import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from steam_family_watchdog_core.config import Config
from steam_family_watchdog_core.errors import SteamError
from steam_family_watchdog_core.monitor import Monitor, next_delay
from steam_family_watchdog_core.steam import SteamApi
from steam_family_watchdog_core.store import Store
from steam_family_watchdog_core.transport import retry_after
from steam_family_watchdog_core.util import iso_time, json_text
from .helpers import A, B, T, FakeAuth, QueueTransport, game, response


class SteamTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_and_invalid_library(self):
        transport = QueueTransport(response({}, 429, {"Retry-After": "1800"}), response({"response": {}}),
                                   response({"response": {"apps": []}}, headers={"x-eresult": "84"}))
        api = SteamApi(FakeAuth(), transport)
        for code in ("RATE_LIMIT", "INVALID_RESPONSE", "RATE_LIMIT"):
            with self.assertRaises(SteamError) as error:
                await api.library("1", "english")
            self.assertEqual(error.exception.code, code)
            if len(transport.calls) == 1:
                self.assertEqual(error.exception.retry_after_seconds, 1800)
        self.assertEqual(retry_after("Thu, 01 Jan 1970 00:01:00 GMT", 0), 60)
        self.assertEqual(retry_after("1.1"), 2)
        self.assertEqual(retry_after("invalid"), 0)
        self.assertEqual(next_delay(Config(), 1, SteamError("RATE_LIMIT", "", 1800), lambda: 0), 1800)
        self.assertEqual(next_delay(Config(), 9, None, lambda: 0), 3600)

    async def test_refresh_once_and_preserve_id_precision(self):
        auth = FakeAuth()
        transport = QueueTransport(response({}, 401), response({"response": {"apps": []}}))
        api = SteamApi(auth, transport)
        self.assertEqual(await api.library("12345678901234567890", "schinese"), [])
        self.assertEqual(auth.renewed, 1)
        self.assertEqual(len(transport.calls), 2)
        for _, _, values in transport.calls:
            params = json.loads(values["params"]["input_json"])
            self.assertEqual(params["family_groupid"], "12345678901234567890")
            self.assertTrue(params["include_own"])
        transport.responses = [response({}, 401), response({}, 401)]
        with self.assertRaises(SteamError) as error:
            await api.library("1", "english")
        self.assertEqual(error.exception.code, "AUTH_REQUIRED")
        self.assertEqual(len(transport.calls), 4)

    async def test_family_and_member_validation(self):
        transport = QueueTransport(
            response({"response": {"family_groupid": "123", "family_group": {"members": [{"steamid": A}]}}}),
            response({"response": {"is_not_member_of_any_group": True}}),
            response({"response": {"family_groupid": 123}}),
            response({"response": {"family_groupid": "123", "family_group": {"members": [{"steamid": int(A)}]}}}),
        )
        api = SteamApi(FakeAuth(), transport)
        self.assertEqual(await api.family(), {"id": "123", "members": [A]})
        for code in ("NO_FAMILY", "INVALID_RESPONSE", "INVALID_RESPONSE"):
            with self.assertRaises(SteamError) as error:
                await api.family()
            self.assertEqual(error.exception.code, code)

    async def test_names_use_public_data(self):
        transport = QueueTransport(response({"response": {"accounts": [
            {"public_data": {"steamid": A, "persona_name": "成员A"}},
            {"public_data": {"steamid": B, "persona_name": "not requested"}}, {},
        ]}}))
        api = SteamApi(FakeAuth(), transport)
        self.assertEqual(await api.names([]), {})
        self.assertEqual(await api.names([A]), {A: "成员A"})

    async def test_bad_json_and_redirect_do_not_update_inventory(self):
        from steam_family_watchdog_core.transport import Response
        api = SteamApi(FakeAuth(), QueueTransport(Response(200, {}, b"not json"), response({}, 302), response({"response": []})))
        for code in ("INVALID_RESPONSE", "STEAM_UNAVAILABLE", "INVALID_RESPONSE"):
            with self.assertRaises(SteamError) as error:
                await api.library("1", "english")
            self.assertEqual(error.exception.code, code)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.fail = None
        self.names_fail = None
        self.library_calls = 0
        self.family_calls = 0
        self.auth = FakeAuth()

        async def family():
            self.family_calls += 1
            return {"id": "1", "members": [A]}

        async def library(*args):
            self.library_calls += 1
            if self.fail:
                raise self.fail
            return [game(10)]

        async def names(*args):
            if self.names_fail:
                raise self.names_fail
            return {A: "成员A"}

        self.api = SimpleNamespace(auth=self.auth, family=family, library=library, names=names)
        self.monitor = Monitor(self.store, self.api, Config(jitter_seconds=0), lambda _: None)

    async def test_client_requests_do_not_trigger_scan_and_failures_keep_baseline(self):
        await self.monitor.scan_once()
        self.store.register("A")
        at = self.store.status()["last_success_at"]
        self.store.changes("A")
        self.store.changes("A")
        self.assertEqual(self.library_calls, 1)
        self.fail = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            await self.monitor.scan_once()
        self.assertEqual(self.store.status()["last_success_at"], at)

    async def test_backoff_status_and_recovery(self):
        self.fail = SteamError("RATE_LIMIT", "限流", 1800)
        self.assertEqual(await self.monitor.tick(), 1800)
        self.assertEqual(self.monitor.status["last_error"]["code"], "RATE_LIMIT")
        self.assertEqual(json.loads(self.store.meta("last_error"))["code"], "RATE_LIMIT")
        self.fail = ValueError("bad response")
        self.assertEqual(await self.monitor.tick(), 1200)
        self.assertEqual(self.monitor.status["last_error"]["code"], "SCAN_FAILED")
        self.fail = None
        self.assertEqual(await self.monitor.tick(), 300)
        self.assertIsNone(self.monitor.status["last_error"])
        self.assertEqual(self.monitor.failures, 0)

    async def test_account_replacement_invalidates_caches(self):
        await self.monitor.scan_once()
        await self.monitor.scan_once()
        self.assertEqual(self.family_calls, 1)
        self.auth.steamid = B
        await self.monitor.scan_once()
        self.assertEqual(self.family_calls, 2)

    async def test_nickname_failure_tolerated_but_rate_limit_stops_scan(self):
        self.names_fail = SteamError("NETWORK_ERROR", "网络")
        await self.monitor.scan_once()
        self.assertTrue(self.store.ready())
        at = self.store.meta("last_success_at")
        self.monitor.names_expires = 0
        self.names_fail = SteamError("RATE_LIMIT", "限流", 900)
        with self.assertRaises(SteamError):
            await self.monitor.scan_once()
        self.assertEqual(self.store.meta("last_success_at"), at)

    async def test_shutdown_waits_for_scan(self):
        entered, resume = asyncio.Event(), asyncio.Event()
        async def library(*args):
            entered.set()
            await resume.wait()
            return [game(10)]
        self.api.library = library
        self.monitor.start()
        await asyncio.wait_for(entered.wait(), 2)
        self.monitor.stop()
        self.assertFalse(self.monitor.task.done())
        resume.set()
        await asyncio.wait_for(self.monitor.wait_stopped(), 2)
        self.assertTrue(self.store.ready())
        self.assertFalse(self.monitor.running)

    async def test_restart_honors_pause_but_new_login_resumes_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Config(data_dir=Path(temp))
            at = iso_time(time.time() - 30)
            self.store.set_meta("last_error", json_text({"code": "AUTH_REQUIRED", "at": at}))
            self.store.set_meta("next_check_at", iso_time(time.time() + 900))
            monitor = Monitor(self.store, self.api, config)
            self.assertGreater(monitor.initial_wait(), 890)
            (config.data_dir / "auth.json").write_text("{}")
            self.assertEqual(monitor.initial_wait(), 0)
            monitor.status["last_error"]["code"] = "RATE_LIMIT"
            self.assertGreater(monitor.initial_wait(), 890)

    async def test_authorization_pause_and_jitter(self):
        self.assertEqual(next_delay(Config(), 1, SteamError("AUTH_REQUIRED", ""), lambda: 0), 900)
        self.assertEqual(next_delay(Config(), 0, None, lambda: 0.999), 310)
