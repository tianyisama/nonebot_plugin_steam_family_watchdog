"""兼容原项目的本地数据 HTTP API。"""

import asyncio
import hmac
import json
import math
import time
from uuid import uuid4

from aiohttp import web

from .store import ApiError, valid_client
from .util import iso_time, json_text


def client_tag(value: object) -> str:
    try:
        return valid_client(value)
    except ApiError:
        return "-"


async def body_json(request: web.Request, limit: int = 32768, require_content_type: bool = True) -> dict:
    if require_content_type and request.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
        raise ApiError(415, "json_required", "POST 请求需要 Content-Type: application/json")
    body = bytearray()
    try:
        async with asyncio.timeout(15):
            async for chunk in request.content.iter_chunked(4096):
                body.extend(chunk)
                if len(body) > limit:
                    raise ApiError(413, "body_too_large", f"请求内容超过 {limit // 1024} KB")
    except TimeoutError as error:
        raise ApiError(408, "request_timeout", "读取请求内容超时") from error
    try:
        value = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeDecodeError) as error:
        raise ApiError(400, "invalid_json", "需要有效的 JSON 对象") from error


def parse_limit(value: object) -> int:
    try:
        if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or not number.is_integer() or not 1 <= number <= 100:
            raise ValueError
        return int(number)
    except (ValueError, TypeError, OverflowError) as error:
        raise ApiError(400, "invalid_limit", "limit 必须为 1～100 的整数") from error


def create_app(store, monitor, auth, secret: str, log=print) -> web.Application:
    routes = {"/health", "/status", "/games", "/changes", "/clients", "/ack"}

    async def handler(request: web.Request) -> web.Response:
        started = time.monotonic()
        request_id = str(uuid4())[:8]
        route = request.path if request.path in routes else "[unknown_route]"
        clientid = client_tag(request.query.get("clientid"))
        result = {}
        status = 500
        log(f"[HTTP] {iso_time()} 收到请求 id={request_id} {request.method} {route} clientid={clientid}")
        try:
            if request.method == "GET" and request.path == "/health":
                status, result = 200, {"service": "Steam Family Monitor", "alive": True}
            elif not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {secret}".encode()):
                status, result = 401, {"success": False, "error": "unauthorized", "message": "需要正确的 Bearer 接口密钥"}
            elif request.method == "GET" and request.path == "/status":
                status, result = 200, {"success": True, **store.status(), **monitor.status,
                    "scanning": monitor.running, "auth_state": auth.state, "steamid": auth.steamid}
            elif request.method == "GET" and request.path == "/games":
                status, result = 200, {"success": True, "family_groupid": store.meta("active_family"), "games": store.games()}
            elif request.path == "/changes" and request.method in ("GET", "POST"):
                params = await body_json(request) if request.method == "POST" else dict(request.query)
                clientid = client_tag(params.get("clientid"))
                data = store.changes(params.get("clientid"), parse_limit(params["limit"]) if "limit" in params else 10)
                status, result = 200, {**data, "last_success_at": store.meta("last_success_at"), "monitor_error": monitor.status["last_error"]}
            elif request.method == "POST" and request.path == "/clients":
                params = await body_json(request)
                clientid = client_tag(params.get("clientid"))
                start = params.get("start") if params.get("start") is not None else "latest"
                status, result = 200, {"success": True, "clientid": params.get("clientid"), **store.register(params.get("clientid"), start)}
            elif request.method == "POST" and request.path == "/ack":
                params = await body_json(request)
                clientid = client_tag(params.get("clientid"))
                status, result = 200, {"clientid": params.get("clientid"), **store.ack(params.get("clientid"), params.get("delivery_id"))}
            else:
                status, result = 404, {"success": False, "error": "not_found", "message": "接口不存在"}
        except ApiError as error:
            status, result = error.status, {"success": False, "error": error.code, "message": str(error)}
        except Exception:
            status, result = 500, {"success": False, "error": "internal_error", "message": "本地处理失败，请检查服务端状态"}
        fields = [f"[HTTP] {iso_time()} 响应 id={request_id}", f"{request.method} {route}",
                  f"clientid={clientid}", f"status={status}", f"耗时={int((time.monotonic() - started) * 1000)}ms"]
        for key, label in (("count", "待通知"), ("cursor", "已确认位置"), ("client_created", "新客户端"),
                           ("delivery_id", "批次"), ("error", "错误")):
            if key in result and result[key] is not None:
                fields.append(f"{label}={result[key]}")
        log(" ".join(fields))
        return web.json_response(result, status=status, dumps=json_text, headers={"Cache-Control": "no-store"})

    app = web.Application(client_max_size=32768)
    app.router.add_route("*", "/{path:.*}", handler)
    return app
