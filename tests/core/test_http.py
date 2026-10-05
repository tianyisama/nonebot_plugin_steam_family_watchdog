import json
import unittest
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from steam_family_watchdog_core.http import create_app
from steam_family_watchdog_core.store import Store
from .helpers import T, game


class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store()
        self.monitor = SimpleNamespace(status={"last_error": None}, running=False)
        self.auth = SimpleNamespace(state="test", steamid=None)
        self.logs = []
        self.client = TestClient(TestServer(create_app(self.store, self.monitor, self.auth, "test-secret", self.logs.append)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()

    async def request(self, url, body=None):
        method = "GET" if body is None else "POST"
        kwargs = {} if body is None else {"json": body}
        return await self.client.request(method, url, headers={"Authorization": "Bearer test-secret"}, **kwargs)

    async def test_auth_get_post_ack_and_independent_clients(self):
        self.assertEqual((await self.client.get("/status")).status, 401)
        self.assertEqual((await self.client.get("/health")).status, 200)
        self.assertEqual((await self.request("/changes?clientid=A")).status, 503)
        self.store.scan("1", [game(10)], T[0])
        self.assertTrue((await (await self.request("/clients", {"clientid": "A"})).json())["created"])
        await self.request("/changes?clientid=B")
        self.store.scan("1", [game(10), game(20)], T[1])
        batch = await (await self.request("/changes", {"clientid": "A", "limit": 5})).json()
        self.assertEqual(batch["count"], 1)
        self.assertEqual(await (await self.request("/changes?clientid=A")).json(), batch)
        self.assertEqual((await self.request("/ack", {"clientid": "A", "delivery_id": "wrong"})).status, 409)
        self.assertEqual((await self.request("/ack", {"clientid": "A", "delivery_id": batch["delivery_id"]})).status, 200)
        self.assertEqual((await (await self.request("/changes?clientid=A")).json())["count"], 0)
        self.assertEqual((await (await self.request("/changes?clientid=B")).json())["count"], 1)
        self.assertEqual((await self.request("/changes?clientid=A&limit=garbage")).status, 400)
        status = await (await self.request("/status")).json()
        self.assertTrue(status["baseline_ready"])
        self.assertNotIn("test-secret", json.dumps(status))
        self.assertTrue(any("收到请求" in line and "GET /changes" in line for line in self.logs))
        self.assertTrue(any("响应" in line and "POST /changes" in line and "clientid=A" in line and "待通知=1" in line for line in self.logs))
        await self.request("/status?access_token=never-log-this&password=never-log-this")
        await self.request("/never-log-this")
        self.assertNotIn("test-secret", "\n".join(self.logs))
        self.assertNotIn("never-log-this", "\n".join(self.logs))

    async def test_body_limits_json_and_unknown_methods(self):
        headers = {"Authorization": "Bearer test-secret", "Content-Type": "application/json"}
        for data, expected in (("[]", 400), ("not-json", 400), ('{"limit":NaN}', 400), ('{"x":"' + 'a' * 32768 + '"}', 413)):
            result = await self.client.post("/changes", headers=headers, data=data)
            self.assertEqual(result.status, expected)
        result = await self.client.post("/changes", headers={"Authorization": "Bearer test-secret"}, data="{}")
        self.assertEqual(result.status, 415)
        self.assertEqual((await self.client.request("PUT", "/status", headers=headers)).status, 404)
        self.assertEqual((await self.request("/unknown")).status, 404)

    async def test_read_only_games_and_validation(self):
        self.store.scan("1", [game(10)], T[0])
        result = await (await self.request("/games")).json()
        self.assertEqual(result["games"][0]["appid"], 10)
        for limit in ("", "0", "101", "nan", "1.2"):
            self.assertEqual((await self.request(f"/changes?clientid=A&limit={limit}")).status, 400)
        self.assertEqual((await self.request("/changes?clientid=A&limit=1.0")).status, 200)
        self.assertEqual((await self.request("/changes", {"clientid": "A", "limit": True})).status, 400)
