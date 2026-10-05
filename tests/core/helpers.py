import base64
import json
import time

from steam_family_watchdog_core.transport import Response

A = "76561198000000001"
B = "76561198000000002"
T = ["2026-10-04T16:00:00.000Z", "2026-10-04T16:05:00.000Z", "2026-10-04T16:10:00.000Z", "2026-10-04T16:15:00.000Z"]
HASH = "f568912870a4684f9ec76277a1a404dda6bab213"


def game(appid, owners=None, acquired=1700000000, images=False):
    value = {"appid": appid, "name": f"Game {appid}", "owner_steamids": [A] if owners is None else owners, "rt_time_acquired": acquired}
    if images:
        value.update(name=f"中文游戏{appid}", capsule_filename="abc123/library_capsule.jpg", img_icon_hash=HASH)
    return value


def jwt(steamid=A, expiry=None, refresh=True, audience=None):
    payload = {"sub": steamid, "exp": expiry or time.time() + 30 * 86400,
               "aud": audience if audience is not None else ["mobile", "derive"] if refresh else ["mobile"]}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{body}.fake-signature"


def response(body, status=200, headers=None):
    return Response(status, headers or {}, json.dumps(body).encode())


class FakeAuth:
    state = "test"
    steamid = A

    def __init__(self):
        self.renewed = 0

    async def access(self, force=False):
        self.renewed += int(force)
        return "secret"


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self):
        pass
