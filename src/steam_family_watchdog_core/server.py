"""独立 HTTP 服务的装配与资源生命周期。"""

import asyncio
import signal
from contextlib import contextmanager

from aiohttp import web

from .http import create_app
from .monitor import Monitor
from .steam import SteamApi, SteamAuth
from .store import Store


@contextmanager
def stop_signals(event: asyncio.Event):
    loop = asyncio.get_running_loop()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda signum, frame: loop.call_soon_threadsafe(event.set))
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def serve(config, stop_event: asyncio.Event | None = None, log=print):
    store = Store(config.data_dir / "monitor.sqlite3", config.missing_confirmations)
    auth = SteamAuth(config.data_dir)
    api = SteamApi(auth)
    monitor = Monitor(store, api, config, log)
    runner = web.AppRunner(create_app(store, monitor, auth, config.api_secret, log), access_log=None, shutdown_timeout=45)
    stop_event = stop_event or asyncio.Event()
    try:
        await runner.setup()
        await web.TCPSite(runner, config.host, config.port).start()
        log(f"Steam 家庭库接口已启动：{config.host}:{config.port}，检查间隔 {config.poll_seconds} 秒。")
        monitor.start()
        with stop_signals(stop_event):
            await stop_event.wait()
    finally:
        monitor.stop()
        try:
            await runner.cleanup()
        finally:
            try:
                await monitor.wait_stopped()
            finally:
                await api.close()
                await auth.close()
                store.close()
