import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from steam_family_watchdog_core.config import acquire_lock, atomic_json, load_config, read_env, setup


class ConfigTests(unittest.TestCase):
    def test_setup_non_destructive_and_root_resolution(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"MONITOR_API_SECRET": ""}):
            root = Path(temp)
            setup(root)
            first = (root / ".env").read_text()
            setup(root)
            self.assertEqual((root / ".env").read_text(), first)
            config = load_config(root)
            self.assertEqual(config.data_dir, root / "data")
            self.assertEqual(len(config.api_secret), 64)
            self.assertEqual(config.poll_seconds, 300)
            atomic_json(root / "config.json", {"poll_seconds": 60})
            self.assertEqual(load_config(root).poll_seconds, 60)

    def test_invalid_config_and_secret(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"MONITOR_API_SECRET": ""}):
            root = Path(temp)
            with self.assertRaises(ValueError):
                load_config(root)
            setup(root)
            for key, value in (("port", 0), ("port", True), ("poll_seconds", 59), ("jitter_seconds", 301),
                               ("member_aliases", []), ("language", None), ("missing_confirmations", 11)):
                atomic_json(root / "config.json", {key: value})
                with self.subTest(key=key), self.assertRaises(ValueError):
                    load_config(root)

    def test_env_quoted_values_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            setup(root)
            (root / ".env").write_text('# comment\nexport MONITOR_API_SECRET="' + 'x' * 32 + '"\nPORT=123 # inline\n', encoding="utf-8")
            self.assertEqual(read_env(root / ".env")["PORT"], "123")
            with patch.dict(os.environ, {"MONITOR_API_SECRET": "y" * 32}):
                self.assertEqual(load_config(root).api_secret, "y" * 32)

    def test_atomic_replace_leaves_no_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "auth.json"
            atomic_json(file, {"name": "中文"})
            atomic_json(file, {"name": "替换"})
            self.assertEqual(json.loads(file.read_text(encoding="utf-8")), {"name": "替换"})
            self.assertEqual([p.name for p in Path(temp).iterdir()], ["auth.json"])

    def test_lock_rejects_current_process_and_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            release = acquire_lock(temp)
            with self.assertRaises(RuntimeError):
                acquire_lock(temp)
            file = Path(temp) / "monitor.lock"
            file.write_text("99999999")
            release()
            self.assertTrue(file.exists())
            file.write_text(str(os.getpid()))
            release()
            self.assertFalse(file.exists())

    def test_dead_process_lock_recovery_and_corrupt_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "monitor.lock"
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            child.wait(timeout=10)
            file.write_text(str(child.pid))
            release = acquire_lock(temp)
            self.assertEqual(file.read_text(), str(os.getpid()))
            release()
            for invalid in ("0", "not a pid", "-1"):
                file.write_text(invalid)
                with self.assertRaises(RuntimeError):
                    acquire_lock(temp)
