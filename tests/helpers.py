import asyncio
import base64
import json
import time
from types import SimpleNamespace

from steam_family_watchdog_core.authentication import save_auth
from steam_family_watchdog_core.errors import SteamError
from steam_family_watchdog_core.steam import SteamAuth

A = "76561198000000001"
T = ["2026-10-04T16:00:00.000Z", "2026-10-04T16:05:00.000Z"]


def token(expiry=None, steamid=A):
    claims = {"sub": steamid, "exp": time.time() + 30 * 86400 if expiry is None else expiry, "aud": ["mobile", "derive"]}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.test-signature"


def game(appid, owners=None):
    return {"appid": appid, "name": f"测试游戏{appid}", "owner_steamids": owners or [A]}


class FakeBot:
    self_id = "12345"
    adapter = SimpleNamespace(get_name=lambda: "OneBot V11")

    def __init__(self):
        self.sent = []
        self.fail_groups = set()
        self.fail_users = set()

    async def send_group_msg(self, group_id, message, **kwargs):
        if str(group_id) in self.fail_groups:
            raise RuntimeError("send failed")
        self.sent.append(("group", str(group_id), str(message)))
        return {"message_id": len(self.sent)}

    async def send_private_msg(self, user_id, message, **kwargs):
        if str(user_id) in self.fail_users:
            raise RuntimeError("send failed")
        self.sent.append(("private", str(user_id), str(message)))
        return {"message_id": len(self.sent)}


class FakeAuth(SteamAuth):
    async def access(self, force=False):
        try:
            self.load()
        except SteamError:
            self.state = "needs_login"
            raise
        self.state = "authenticated"
        return "test-access"


class FakeApi:
    def __init__(self):
        self.auth = None
        self.apps = [game(10)]
        self.failure = None
        self.calls = 0
        self.entered = self.resume = None
        self.closed = False

    def factory(self, auth):
        self.auth = auth
        self.closed = False
        return self

    async def family(self):
        return {"id": "1", "members": [A]}

    async def library(self, *args):
        self.calls += 1
        if self.entered:
            self.entered.set()
            await self.resume.wait()
        if self.failure:
            raise self.failure
        return self.apps

    async def names(self, *args):
        return {A: "家庭成员A"}

    async def close(self):
        self.closed = True
