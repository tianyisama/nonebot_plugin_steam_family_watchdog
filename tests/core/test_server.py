import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession, ClientConnectorError

from steam_family_watchdog_core.config import acquire_lock, atomic_json, load_config, setup
from steam_family_watchdog_core.server import serve


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_data_dir_override_matches_plugin_directory(self):
        from steam_family_watchdog_core.__main__ import _run
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = root / "steam-login"
            target = root / "bot-data"
            setup(settings)
            received = []
            async def fake_serve(config):
                received.append(config.data_dir)
                self.assertTrue((target / "monitor.lock").exists())
            with patch("steam_family_watchdog_core.__main__.serve", fake_serve):
                await _run(SimpleNamespace(command="start", root=settings, data_dir=target))
            self.assertEqual(received, [target.resolve()])
            self.assertFalse((target / "monitor.lock").exists())

    async def test_assembled_service_health_missing_login_and_graceful_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup(root)
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            atomic_json(root / "config.json", {"host": "127.0.0.1", "port": port, "jitter_seconds": 0})
            config = load_config(root)
            release = acquire_lock(config.data_dir)
            stop_event = asyncio.Event()
            logs = []
            task = asyncio.create_task(serve(config, stop_event, logs.append))
            try:
                async with ClientSession() as client:
                    async with asyncio.timeout(3):
                        while True:
                            try:
                                async with client.get(f"http://127.0.0.1:{port}/health") as response:
                                    self.assertTrue((await response.json())["alive"])
                                    break
                            except ClientConnectorError:
                                if task.done():
                                    await task
                                await asyncio.sleep(0.01)
                    headers = {"Authorization": f"Bearer {config.api_secret}"}
                    async with client.get(f"http://127.0.0.1:{port}/status", headers=headers) as response:
                        status = await response.json()
                        self.assertFalse(status["baseline_ready"])
                        self.assertEqual(status["auth_state"], "needs_login")
                        self.assertEqual(status["last_error"]["code"], "AUTH_REQUIRED")
                    async with client.get(f"http://127.0.0.1:{port}/changes?clientid=A", headers=headers) as response:
                        self.assertEqual(response.status, 503)
            finally:
                stop_event.set()
                try:
                    await asyncio.wait_for(task, 3)
                finally:
                    release()
            self.assertFalse((config.data_dir / "monitor.lock").exists())
            self.assertNotIn(config.api_secret, "\n".join(logs))
            self.assertTrue((config.data_dir / "monitor.sqlite3").exists())

    async def test_installed_cli_setup_and_help(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {**os.environ, "PYTHONUTF8": "1"}
            result = await asyncio.to_thread(subprocess.run,
                [sys.executable, "-m", "steam_family_watchdog_core", "setup", "--root", temp],
                text=True, encoding="utf-8", capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("配置已准备好", result.stdout)
            self.assertEqual(json.loads((Path(temp) / "config.json").read_text())["port"], 11452)
            result = await asyncio.to_thread(subprocess.run,
                [sys.executable, "-m", "steam_family_watchdog_core", "--help"],
                text=True, encoding="utf-8", capture_output=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertIn("login-qr", result.stdout)
