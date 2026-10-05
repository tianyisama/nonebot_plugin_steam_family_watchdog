"""本地管理页面预览，使用模拟 Bot，不发送真实消息；回车退出。"""

import asyncio
import sys
import tempfile
from pathlib import Path

from steam_family_watchdog_core.authentication import save_auth
from nonebot_plugin_steam_family_watchdog.config import Config
from nonebot_plugin_steam_family_watchdog.service import Service
from nonebot_plugin_steam_family_watchdog.web import WebManager
from tests.helpers import FakeApi, FakeAuth, FakeBot, token


async def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        save_auth(root / "auth.json", token())
        bot, api = FakeBot(), FakeApi()
        config = Config(steam_family_data_dir=root, steam_family_push_groups=["100", "200"],
            steam_family_push_users=["300"], steam_family_web_host="0.0.0.0", steam_family_web_port=11464,
            steam_family_web_token="preview-" + "x" * 56)
        service = Service(config, lambda: {bot.self_id: bot}, lambda: {"999"}, auth_factory=FakeAuth, api_factory=api.factory)
        await service.startup()
        manager = WebManager(service)
        await manager.start()
        try:
            print("PREVIEW_READY http://127.0.0.1:11464", flush=True)
            await asyncio.to_thread(sys.stdin.readline)
        finally:
            await manager.close()
            await service.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
