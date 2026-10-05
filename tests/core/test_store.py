import json
import tempfile
import unittest
from pathlib import Path

from steam_family_watchdog_core.artwork import artwork
from steam_family_watchdog_core.store import ApiError, Store, normalize_apps
from .helpers import A, B, HASH, T, game


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def test_silent_baseline_game_and_owner_events(self):
        db = self.store
        self.assertEqual(db.scan("1", [game(10)], T[0], [A, B])["event_count"], 0)
        db.save_names({A: "成员A", B: "成员B"}, T[0])
        db.register("groupA")
        self.assertEqual(db.scan("1", [game(10, [A, B]), game(20, [B])], T[1], [A, B])["event_count"], 2)
        events = db.changes("groupA")["new_games"]
        self.assertEqual([event["type"] for event in events], ["owner_added", "game_added"])
        self.assertEqual(events[0]["added_owners"], [{"steamid": B, "name": "成员B"}])
        self.assertEqual(events[0]["observed_after"], T[0])
        self.assertEqual(events[0]["detected_at"], T[1])
        self.assertEqual(events[0]["steam_acquired_at"], "2023-11-14T22:13:20.000Z")
        self.assertIs(events[0]["acquired_time_verified"], False)

    def test_clients_independent_ack_idempotent_no_skip(self):
        db = self.store
        db.scan("1", [game(10)], T[0])
        db.register("A")
        db.register("B")
        db.scan("1", [game(10), game(20)], T[1])
        a, b = db.changes("A"), db.changes("B")
        self.assertEqual(db.changes("A"), a)
        self.assertNotEqual(a["delivery_id"], b["delivery_id"])
        with self.assertRaises(ApiError) as error:
            db.ack("A", b["delivery_id"])
        self.assertEqual(error.exception.code, "delivery_mismatch")
        db.scan("1", [game(10), game(20), game(30)], T[2])
        repeated = db.changes("A")
        self.assertEqual(repeated["delivery_id"], a["delivery_id"])
        self.assertEqual(repeated["new_games"], a["new_games"])
        self.assertTrue(repeated["has_more"])
        db.ack("A", a["delivery_id"])
        self.assertTrue(db.ack("A", a["delivery_id"])["already_acked"])
        self.assertEqual([e["appid"] for e in db.changes("A")["new_games"]], [30])
        self.assertEqual([e["appid"] for e in db.changes("B")["new_games"]], [20])

    def test_reopen_pagination_and_start_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "monitor.db"
            with Store(file) as db:
                db.scan("1", [game(10)], T[0])
                db.scan("1", [game(10), game(20), game(30)], T[1])
                self.assertEqual(db.changes("late")["count"], 2)
                db.register("latest")
                self.assertEqual(db.changes("latest")["count"], 0)
                db.register("replay", "beginning")
                pending = db.changes("replay", 1)
                self.assertTrue(pending["has_more"])
            with Store(file) as db:
                self.assertEqual(db.changes("replay", 100), pending)
                db.ack("replay", pending["delivery_id"])
                self.assertEqual([e["appid"] for e in db.changes("replay")["new_games"]], [30])
                self.assertEqual(db.scan("1", [game(10), game(20), game(30)], T[2])["event_count"], 0)

    def test_invalid_response_preserves_snapshot_and_time(self):
        db = self.store
        db.scan("1", [game(10)], T[0])
        db.register("A")
        malformed = [None, {}, [game(10), game(10)], [{**game(10), "owner_steamids": [int(A)]}],
                     [{"appid": True}], [{**game(10), "owner_steamids": None}]]
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                db.scan("1", value, T[1])
        self.assertEqual([g["appid"] for g in db.games()], [10])
        self.assertEqual(db.status()["last_success_at"], T[0])
        self.assertEqual(db.changes("A")["count"], 0)

    def test_failed_write_rolls_back_entire_scan(self):
        db = self.store
        db.scan("1", [game(10)], T[0])
        db.db.execute("CREATE TRIGGER deny_game BEFORE INSERT ON games WHEN NEW.appid=30 BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            db.scan("1", [game(10), game(20), game(30)], T[1])
        self.assertEqual(db.status()["event_count"], 0)
        self.assertEqual(db.status()["last_success_at"], T[0])
        self.assertEqual([g["appid"] for g in db.games()], [10])

    def test_transient_and_confirmed_disappearance(self):
        db = self.store
        db.scan("1", [game(10)], T[0])
        db.scan("1", [], T[1])
        self.assertEqual(db.games(), [])
        self.assertEqual(db.scan("1", [game(10)], T[2])["event_count"], 0)
        db.scan("1", [], T[2])
        db.scan("1", [], T[3])
        self.assertEqual(db.scan("1", [game(10)], T[3])["event_count"], 1)

    def test_unknown_and_missing_owners(self):
        db = self.store
        db.scan("1", [game(10, [A, B])], T[0])
        db.scan("1", [game(10, [A])], T[1])
        self.assertEqual(db.scan("1", [game(10, [A, B])], T[2])["event_count"], 0)
        db.scan("1", [game(10, [])], T[2])
        self.assertEqual(db.games()[0]["owner_steamids"], [A, B])
        self.assertEqual(db.scan("1", [game(10, [A, B])], T[3])["event_count"], 0)
        db.scan("1", [game(10, [A])], T[2])
        db.scan("1", [game(10, [A])], T[3])
        self.assertEqual(db.scan("1", [game(10, [A, B])], T[3])["event_count"], 1)

    def test_family_baselines_member_join_and_aliases(self):
        db = self.store
        db.scan("1", [game(10)], T[0], [A])
        db.scan("1", [game(10), game(20, [B])], T[1], [A, B], {B: "别名"})
        event = db.changes("A")["new_games"][0]
        self.assertEqual(event["source_context"], "member_joined")
        self.assertEqual(event["added_owners"][0]["name"], "别名")
        self.assertEqual(db.scan("2", [game(100)], T[2], [A])["event_count"], 0)

    def test_validation(self):
        db = self.store
        with self.assertRaises(ApiError) as error:
            db.register("A")
        self.assertEqual(error.exception.code, "not_ready")
        for id_ in ("../x", "", "a\n", 1, "中"):
            with self.assertRaises(ApiError):
                db.changes(id_)
        db.scan("1", [game(10)], T[0])
        for limit in (0, 101, True, 1.0):
            with self.assertRaises(ApiError):
                db.changes("A", limit)
        with self.assertRaises(ApiError):
            db.register("A", "invalid")
        with self.assertRaises(ApiError):
            db.ack("unknown", "wrong")

    def test_normalization_filtering(self):
        apps = normalize_apps([game(10, [B, A, B]), {"appid": 20, "exclude_reason": 1}, {"appid": 30, "app_type": 2}])
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["owners"], [A, B])
        value = normalize_apps([{"appid": 40, "name": " ", "rt_time_acquired": 0}])[0]
        self.assertEqual(value["name"], "App 40")
        self.assertIsNone(value["rt_time_acquired"])

    def test_198_199_199_keeps_history_for_late_clients(self):
        db = self.store
        baseline = [game(i + 1) for i in range(198)]
        self.assertTrue(db.scan("1", baseline, T[0], [A, B])["baseline_created"])
        db.changes("early")
        expanded = [*baseline, game(999, [B], 1700001000)]
        self.assertEqual(db.scan("1", expanded, T[1], [A, B])["event_count"], 1)
        before = db.changes("early")
        self.assertEqual(db.scan("1", expanded, T[2])["event_count"], 0)
        after = db.changes("early")
        self.assertEqual(after["new_games"], before["new_games"])
        self.assertEqual(after["delivery_id"], before["delivery_id"])
        self.assertEqual(after["new_games"][0]["detected_at"], T[1])
        self.assertEqual(db.changes("late")["count"], 1)
        db.ack("early", after["delivery_id"])
        self.assertEqual(db.changes("early")["count"], 0)
        self.assertEqual(db.changes("late")["count"], 1)
        self.assertEqual(db.status()["event_count"], 1)

    def test_artwork_paths_and_preference(self):
        images = artwork(440, "library_600x900.jpg", HASH.upper())
        self.assertEqual(images["capsule_image_url"], "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/440/library_600x900.jpg")
        self.assertTrue(images["icon_image_url"].endswith(f"/{HASH}.jpg"))
        self.assertEqual(images["image_url"], images["capsule_image_url"])
        for file in ("../secret.png", "https://evil.invalid/image.png", "a/../../b.jpg", "a\\b.jpg", "a/./b.jpg", "a/" + "b" * 512):
            self.assertIsNone(artwork(440, file, HASH)["capsule_image_url"])
        self.assertEqual(artwork(440, None, HASH)["image_url"], images["icon_image_url"])
        self.assertIsNone(artwork(440, None, "invalid")["image_url"])

    def test_artwork_and_chinese_names_survive_delivery(self):
        db = self.store
        db.scan("1", [game(10, images=True)], T[0])
        db.changes("A")
        db.scan("1", [game(10, images=True), game(20, images=True)], T[1])
        batch = db.changes("A")
        self.assertEqual(batch["new_games"][0]["name"], "中文游戏20")
        self.assertTrue(batch["new_games"][0]["capsule_image_url"].endswith("/20/abc123/library_capsule.jpg"))
        self.assertEqual(db.games()[0]["img_icon_hash"], HASH)
        self.assertEqual(db.scan("1", [game(10, images=True), game(20, images=True)], T[2])["event_count"], 0)
        self.assertEqual(db.changes("A")["new_games"], batch["new_games"])

    def test_legacy_event_images_do_not_rewrite_history(self):
        db = self.store
        db.scan("1", [game(10, images=True)], T[0])
        db.scan("1", [game(10, images=True), game(20, images=True)], T[1])
        batch = db.changes("A")
        original = dict(batch["new_games"][0])
        for key in ("event_id", *artwork(20)):
            original.pop(key)
        legacy = json.dumps(original)
        db.db.execute("UPDATE events SET payload=? WHERE id=?", (legacy, batch["new_games"][0]["event_id"]))
        upgraded = db.changes("A")
        self.assertEqual(upgraded["delivery_id"], batch["delivery_id"])
        self.assertEqual(upgraded["cursor"], batch["cursor"])
        self.assertTrue(upgraded["new_games"][0]["image_url"])
        self.assertEqual(db.db.execute("SELECT payload FROM events").fetchone()[0], legacy)

    def test_legacy_schema_migration_keeps_pending_clients(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "monitor.db"
            with Store(file) as db:
                db.scan("1", [game(10)], T[0])
                db.scan("1", [game(10), game(20)], T[1])
                pending = db.changes("A")
                db.db.execute("DROP TABLE game_artwork")
            with Store(file) as db:
                self.assertEqual([row["name"] for row in db.db.execute("PRAGMA table_info(games)")],
                                 ["family_id", "appid", "name", "owners", "acquired", "missing"])
                self.assertEqual(db.status()["event_count"], 1)
                self.assertEqual(len(db.games()), 2)
                self.assertEqual(db.changes("A")["delivery_id"], pending["delivery_id"])
