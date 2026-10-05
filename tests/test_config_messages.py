import unittest

from pydantic import ValidationError
from nonebot_plugin_steam_family_watchdog.config import Config, public_config
from nonebot_plugin_steam_family_watchdog.messages import notification, superuser_ids
from .helpers import A, T


class ConfigMessageTests(unittest.TestCase):
    def test_ids_lists_defaults_and_hidden_secrets(self):
        config = Config(steam_family_push_groups=[123, "123", "00456"], steam_family_web_token="secret-" * 8)
        self.assertEqual(config.steam_family_push_groups, ["123", "456"])
        self.assertTrue(config.steam_family_data_dir.is_absolute())
        self.assertNotIn(config.steam_family_web_token, repr(config))
        self.assertNotIn("steam_family_web_token", public_config(config))
        self.assertEqual(config.steam_family_web_host, "0.0.0.0")

    def test_strict_bounds_and_alias_validation(self):
        for values in ({"steam_family_poll_seconds": True}, {"steam_family_poll_seconds": 60.1},
                       {"steam_family_push_groups": [False]}, {"steam_family_push_users": ["-1"]},
                       {"steam_family_web_token": "short"}, {"steam_family_member_aliases": {"1": "name"}}):
            with self.assertRaises(ValidationError):
                Config(**values)

    def test_superuser_adapter_prefixes(self):
        self.assertEqual(superuser_ids({"123", "OneBot V11:456", "Console:789", "abc", "OneBot V11:123"}), ["123", "456"])

    def test_notification_escapes_cq_names_and_formats_beijing_time(self):
        event = {"type": "owner_added", "appid": 440, "name": "[CQ:at,qq=all]",
                 "added_owners": [{"steamid": A, "name": "成员A"}], "detected_at": T[0],
                 "source_context": "member_joined", "image_url": "https://example.invalid/cover.jpg"}
        message = notification([event])
        self.assertEqual(message[0].type, "text")
        self.assertIn("2026-10-05 00:00:00", message.extract_plain_text())
        self.assertIn("新增拥有者", message.extract_plain_text())
        self.assertEqual([segment.type for segment in message], ["text", "image"])
        self.assertEqual([segment.type for segment in notification([event], False)], ["text"])
