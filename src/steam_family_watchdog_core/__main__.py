"""python -m steam_family_watchdog_core [setup|login|login-qr|start]"""

import argparse
import asyncio
import os
import sys
from dataclasses import replace
from pathlib import Path

from aiohttp import web

from .config import acquire_lock, load_config, read_env, setup
from .errors import SteamError
from .login import login_qr
from .login_web import LoginWeb
from .server import serve, stop_signals


async def _run(args):
    if args.command == "setup":
        setup(args.root)
        print("配置已准备好。接口密钥保存在 .env；首次使用请运行 python -m steam_family_watchdog_core login。")
        return
    config = load_config(args.root)
    if args.data_dir is not None:
        data_dir = args.data_dir.expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = replace(config, data_dir=data_dir)
    release = acquire_lock(config.data_dir)
    try:
        if args.command == "start":
            await serve(config)
        elif args.command == "login-qr":
            await login_qr(config)
        else:
            port_text = os.environ.get("MONITOR_LOGIN_PORT") or read_env(args.root / ".env").get("MONITOR_LOGIN_PORT", "11453")
            login = LoginWeb(config, int(port_text))
            runner = web.AppRunner(login.create_app(), access_log=None, shutdown_timeout=45)
            try:
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", login.port).start()
                print(f"请在本机浏览器打开 {login.origin}，使用 Steam++ 动态验证码登录。")
                with stop_signals(login.stop_event):
                    await login.stop_event.wait()
            finally:
                await runner.cleanup()
    finally:
        release()


def main():
    parser = argparse.ArgumentParser(description="Steam 家庭库监控工具（Python 版）")
    parser.add_argument("command", nargs="?", choices=("setup", "login", "login-qr", "start"), default="start")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="配置和 .env 所在目录；默认为当前目录")
    parser.add_argument("--data-dir", type=Path, help="覆盖数据目录，相对于当前工作目录解析；可用于直接登录插件的数据目录")
    args = parser.parse_args()
    args.root = args.root.resolve()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    except SteamError as error:
        print(f"{error.code}：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("启动失败：请检查配置、网络、端口、数据库权限及实例锁。", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
