"""Offline v2 persistence, identity isolation, and daily queue regression tests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from spark_assistant.scheduler import Scheduler
from spark_assistant.store import CHINA_TZ, Store


def at(day=16, hour=8, minute=0):
    return datetime(2035, 10, day, hour, minute, tzinfo=CHINA_TZ)


def directory(*ids, account="owner", coverage="complete"):
    return {"account_validated": True, "account": {"platform_id": account, "verification": "verified"},
            "friends": [{"platform_id": key, "profile_url": f"https://www.douyin.com/user/{key}",
                         "display_name": "同名好友", "relationship": "existing_chat", "source": "official_chat_ui",
                         "conversation_id": f"0:1:owner:{key}", "conversation_type": "one_to_one",
                         "identity_verified": True, "dm_verified": True} for key in (ids or ("friend-a", "friend-b"))],
            "coverage": {"status": coverage, "directory_kind": "existing_chats"}}


def create_v1(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE contacts(name TEXT PRIMARY KEY,position INTEGER NOT NULL);
            CREATE TABLE rules(id TEXT PRIMARY KEY,recipient TEXT NOT NULL,message TEXT NOT NULL,
                send_time TEXT NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                effective_from TEXT NOT NULL,next_scheduled_at TEXT NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,recipient TEXT NOT NULL,message TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,scheduled_date TEXT NOT NULL,started_at TEXT,finished_at TEXT,
                status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',UNIQUE(rule_id,scheduled_date));
            INSERT INTO contacts VALUES('同名好友',0);
            INSERT INTO settings VALUES('account_ready','true');
            PRAGMA user_version=1;
        """)
        conn.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?)", ("old", "同名好友", "旧正文", "08:00", 1,
                     at(16, 7).isoformat(), at(16, 7).isoformat(), at(16, 7).isoformat(), at().isoformat()))
        for key, day, status in (("done", 14, "persisted_in_chat"), ("working", 15, "running"), ("queued", 16, "pending")):
            conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,NULL,NULL,?,'')", (key, "old", "同名好友", "旧正文", at(day).isoformat(), at(day).date().isoformat(), status))
        conn.commit()


class StoreSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "app.db"
        self.clock = patch("spark_assistant.store.local_now", return_value=at(16, 7))
        self.scheduler_clock = patch("spark_assistant.scheduler.local_now", return_value=at())
        self.clock.start()
        self.scheduler_clock.start()
        self.store = Store(self.path)
        self.store.apply_friend_sync(directory(), account_epoch=1)
        self.store.set_setting("account_ready", True)
        self.scheduler = Scheduler(self.store)

    def tearDown(self):
        self.store.close()
        self.clock.stop()
        self.scheduler_clock.stop()
        self.tmp.cleanup()

    def rule(self, now=None, *, contact=0, message="你好", time="08:00", enabled=True, **kw):
        with patch("spark_assistant.store.local_now", return_value=now or at(16, 7)):
            return self.store.save_rule("同名好友", message, time, enabled,
                                        contact_id=self.store.list_contacts()[contact]["id"], **kw)

    def close_run(self, run, status="persisted_in_chat", **kw):
        self.scheduler.complete(run["id"], status, **kw)

    def restart(self):
        self.store.close()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store)
        self.scheduler.recover_interrupted()

    def test_settings_and_structured_contacts_persist(self):
        rule = self.rule()
        contacts = self.store.list_contacts()
        self.store.set_setting("object", {"text": "你好", "enabled": False})
        self.restart()
        self.assertEqual(contacts, self.store.list_contacts())
        self.assertEqual({"text": "你好", "enabled": False}, self.store.get_setting("object"))
        self.assertEqual(rule, self.store.list_rules()[0]["id"])
        self.assertIs(self.store.list_rules()[0]["enabled"], True)

    def test_same_names_keep_distinct_stable_identity(self):
        contacts = self.store.list_contacts()
        self.assertEqual(contacts[0]["display_name"], contacts[1]["display_name"])
        self.assertNotEqual(contacts[0]["id"], contacts[1]["id"])
        self.assertTrue(all(row["sendable"] for row in contacts))

    def test_existing_chat_directory_keeps_its_actual_scope(self):
        meta = self.store.get_friend_sync()
        self.assertEqual("existing_chats", meta["directory_kind"])
        contacts = self.store.list_contacts()
        self.assertTrue(all(row["source"] == "official_chat_ui" and row["relationship"] == "existing_chat" for row in contacts))
        self.rule()
        self.assertIsNotNone(self.scheduler.tick(at()))

    def test_conversation_route_is_persisted_in_rule_view_and_run_snapshot(self):
        rule = self.rule()
        current = self.store.list_rules()[0]
        self.assertEqual("0:1:owner:friend-a", current["conversation_id"])
        run = self.scheduler.enqueue_manual([rule], at())["accepted"][0]
        self.assertEqual(current["conversation_id"], run["conversation_id"])
        self.assertEqual("one_to_one", run["conversation_type"])
        self.restart()
        self.assertEqual(run["conversation_id"], self.store.today_runs(at())[rule]["conversation_id"])

    def test_missing_conversation_id_is_not_sendable_even_if_dm_flag_true(self):
        result = directory("friend-a")
        del result["friends"][0]["conversation_id"]
        meta = self.store.apply_friend_sync(result)
        contact = next(c for c in self.store.list_contacts() if c["platform_id"] == "friend-a")
        self.assertEqual("partial", meta["completeness"])
        self.assertFalse(contact["sendable"])
        self.assertFalse(contact["dm_verified"])
        with self.assertRaisesRegex(ValueError, "会话标识"):
            self.store.save_rule("同名好友", "hi", contact_id=contact["id"])

    def test_missing_conversation_type_never_implies_private(self):
        result = directory("friend-a")
        del result["friends"][0]["conversation_type"]
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("partial", meta["completeness"])
        self.assertEqual(0, meta["sendable_count"])

    def test_resync_changed_conversation_cancels_manual_without_retarget(self):
        rule = self.rule()
        original = self.scheduler.enqueue_manual([rule], at())["accepted"][0]
        result = directory()
        result["friends"][0]["conversation_id"] = "0:1:new-conversation"
        self.store.apply_friend_sync(result)
        run = self.store.today_runs(at())[rule]
        self.assertEqual("cancelled", run["status"])
        self.assertEqual("conversation_changed", run["code"])
        self.assertEqual(original["conversation_id"], run["conversation_id"])
        self.assertIsNone(self.scheduler.tick(at()))
        new = self.scheduler.enqueue_manual([rule], at())["accepted"][0]
        self.assertNotEqual(original["id"], new["id"])
        self.assertEqual("0:1:new-conversation", new["conversation_id"])
        self.assertEqual(original["conversation_id"], self.store.today_rule_states(at())[rule]["daily"]["conversation_id"])

    def test_failed_safe_retry_cannot_move_to_new_conversation(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        run = self.scheduler.tick(at())
        self.close_run(run, "failed", submission="not_submitted")
        result = directory()
        result["friends"][0]["conversation_id"] = "0:1:replacement"
        self.store.apply_friend_sync(result)
        with self.assertRaisesRegex(ValueError, "变化"):
            self.scheduler.retry_run(run["id"], at())
        self.assertEqual([], self.scheduler.enqueue_manual([rule], at())["accepted"])

    def test_legacy_mutual_fixture_remains_explicitly_labeled(self):
        result = directory("legacy-friend")
        result["coverage"]["directory_kind"] = "mutual_friends"
        result["friends"][0].update(source="official_mutual_ui", relationship="mutual")
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("mutual_friends", meta["directory_kind"])
        current = next(c for c in self.store.list_contacts() if c["platform_id"] == "legacy-friend")
        self.assertTrue(current["sendable"])
        self.assertEqual("mutual", current["relationship"])

    def test_group_and_unknown_chat_routes_cannot_create_tasks(self):
        for marker in ({"is_group": True}, {"conversation_type": "group"}, {"conversation_kind": "unknown"},
                       {"conversation_type": 2}, {"conversation_type": ["invalid"]}):
            result = directory("group-id")
            result["friends"][0].update(marker)
            meta = self.store.apply_friend_sync(result)
            self.assertEqual(0, meta["sendable_count"])
            self.assertFalse(any(c["platform_id"] == "group-id" for c in self.store.list_contacts()))

    def test_chat_source_cannot_be_reported_as_mutual_directory(self):
        result = directory("friend-a")
        result["coverage"]["directory_kind"] = "mutual_friends"
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("partial", meta["completeness"])
        self.assertEqual(0, meta["sendable_count"])

    def test_name_only_never_authorizes_send(self):
        self.store.replace_contacts(["同名好友", "955"])
        old = self.store.save_rule("同名好友", "hi", "08:00", True)
        self.assertFalse(self.store.list_rules()[0]["sendable"])
        self.assertIsNone(self.scheduler.tick(at()))
        self.assertEqual([], self.scheduler.enqueue_manual([old], at())["accepted"])

    def test_missing_account_identity_is_fail_closed(self):
        self.rule()
        result = directory()
        del result["account"]["platform_id"]
        self.store.apply_friend_sync(result)
        self.store.set_setting("account_ready", True)
        self.assertIsNone(self.scheduler.tick(at()))
        self.assertTrue(all(not contact["sendable"] for contact in self.store.list_contacts()))

    def test_identity_and_dm_capabilities_are_required(self):
        result = directory()
        result["friends"][0]["identity_verified"] = False
        result["friends"][1]["dm_verified"] = False
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("partial", meta["completeness"])
        self.assertEqual(0, meta["sendable_count"])
        self.assertTrue(all(not row["sendable"] for row in self.store.list_contacts()))

    def test_partial_does_not_claim_unobserved_friend_was_removed(self):
        meta = self.store.apply_friend_sync(directory("friend-a", coverage="partial"))
        self.assertEqual("partial", meta["completeness"])
        contacts = {c["platform_id"]: c for c in self.store.list_contacts()}
        self.assertTrue(contacts["friend-a"]["sendable"])
        self.assertEqual("stale", contacts["friend-b"]["sync_state"])
        self.assertFalse(contacts["friend-b"]["sendable"])

    def test_specific_friend_blocked_reason_is_preserved(self):
        result = directory("friend-a")
        result["friends"][0].update(dm_verified=False, blocked_reason="官网未显示该好友的私信入口")
        self.store.apply_friend_sync(result)
        contact = next(c for c in self.store.list_contacts() if c["platform_id"] == "friend-a")
        self.assertFalse(contact["sendable"])
        self.assertEqual("官网未显示该好友的私信入口", contact["blocked_reason"])

    def test_complete_marks_absent_friend_missing(self):
        self.store.apply_friend_sync(directory("friend-a"))
        contacts = {c["platform_id"]: c for c in self.store.list_contacts()}
        self.assertEqual("missing", contacts["friend-b"]["sync_state"])

    def test_malformed_complete_degrades_to_partial_without_deleting(self):
        result = directory("friend-a")
        result["friends"].append({"display_name": "没有身份"})
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("partial", meta["completeness"])
        self.assertEqual(1, meta["unverified_count"])
        contacts = {c["platform_id"]: c for c in self.store.list_contacts()}
        self.assertEqual("stale", contacts["friend-b"]["sync_state"])

    def test_conflicting_duplicate_cannot_authorize_identity(self):
        result = directory("friend-a")
        conflict = deepcopy(result["friends"][0])
        conflict["display_name"] = "另一个显示名"
        result["friends"].append(conflict)
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("partial", meta["completeness"])
        self.assertEqual(0, meta["sendable_count"])

    def test_conflicting_relationship_for_same_id_revokes_first_record(self):
        result = directory("friend-a")
        conflict = deepcopy(result["friends"][0])
        conflict["relationship"] = "following_only"
        result["friends"].append(conflict)
        meta = self.store.apply_friend_sync(result)
        self.assertEqual("partial", meta["completeness"])
        self.assertEqual(0, meta["sendable_count"])
        result["friends"].reverse()
        self.assertEqual(0, self.store.apply_friend_sync(result)["sendable_count"])

    def test_failed_sync_preserves_names_but_blocks_stale_records(self):
        result = directory(coverage="failed")
        self.store.apply_friend_sync(result)
        self.assertEqual(2, len(self.store.list_contacts()))
        self.assertTrue(all(c["sync_state"] == "stale" for c in self.store.list_contacts()))
        self.assertEqual(0, self.store.get_friend_sync()["sendable_count"])

    def test_stale_sync_result_rejected_after_invalidation(self):
        old = self.store.get_account()["account_epoch"]
        self.store.invalidate_account_epoch()
        with self.assertRaisesRegex(ValueError, "旧登录"):
            self.store.apply_friend_sync(directory(), account_epoch=old)
        self.assertEqual(0, self.store.get_friend_sync()["sendable_count"])

    def test_first_login_invalidation_does_not_require_empty_rule_review(self):
        self.store.invalidate_account_epoch()
        self.assertFalse(self.store.get_setting("login_review_required"))

    def test_account_switch_cancels_old_queue_and_never_rebinds_names(self):
        rule = self.rule()
        old = self.scheduler.enqueue_manual([rule], at())["accepted"][0]
        meta = self.store.apply_friend_sync(directory(account="other"), account_epoch=1)
        self.assertTrue(meta["account_changed"])
        self.assertEqual(2, self.store.get_account()["account_epoch"])
        self.assertEqual("cancelled", self.store.today_runs(at())[rule]["status"])
        self.assertEqual(old["account_id"], "owner")
        self.assertTrue(self.store.list_rules()[0]["needs_review"])
        self.assertFalse(self.store.list_rules()[0]["sendable"])
        self.store.set_setting("account_ready", True)
        self.assertIsNone(self.scheduler.tick(at()))

    def test_rebinding_requires_explicit_contact_and_does_not_rewrite_today(self):
        rule = self.rule()
        original = self.scheduler.enqueue_manual([rule], at())["accepted"][0]
        self.store.invalidate_account_epoch()
        self.store.apply_friend_sync(directory(), account_epoch=2)
        self.store.save_rule("同名好友", "你好", "08:00", True, rule_id=rule)
        self.assertTrue(self.store.list_rules()[0]["needs_review"])
        self.rule(now=at(16, 9), rule_id=rule)
        self.assertFalse(self.store.list_rules()[0]["needs_review"])
        queued = self.scheduler.enqueue_manual([rule], at(16, 9))
        self.assertNotEqual(original["id"], queued["accepted"][0]["id"])
        self.assertEqual(2, queued["accepted"][0]["account_epoch"])
        self.assertEqual(original["id"], self.store.today_rule_states(at())[rule]["daily"]["id"])

    def test_no_early_or_retroactive_timer_send(self):
        self.rule(now=at(16, 9))
        self.assertIsNone(self.scheduler.tick(at(16, 10)))
        self.assertEqual([], self.store.list_runs())
        self.assertIsNone(self.scheduler.tick(at(17, 7, 59)))
        self.assertIsNotNone(self.scheduler.tick(at(17)))

    def test_same_day_single_claim_survives_restart(self):
        self.rule()
        first = self.scheduler.tick(at())
        self.assertIsNone(self.scheduler.tick(at(16, 9)))
        self.close_run(first)
        self.restart()
        self.assertIsNone(self.scheduler.tick(at(16, 10)))
        self.assertNotEqual(first["id"], self.scheduler.tick(at(17))["id"])

    def test_same_friend_multiple_configurations_are_distinct_serial_jobs(self):
        a, b = self.rule(), self.rule()
        result = self.scheduler.enqueue_manual([a, b, a], at(16, 7, 30))
        self.assertEqual(2, len(result["accepted"]))
        first = self.scheduler.tick(at(16, 7, 30))
        self.assertEqual(a, first["rule_id"])
        self.assertIsNone(self.scheduler.tick(at(16, 7, 30)))
        self.close_run(first)
        self.assertEqual(b, self.scheduler.tick(at(16, 7, 30))["rule_id"])

    def test_manual_early_consumes_scheduled_occurrence_and_preserves_time(self):
        rule = self.rule(time="21:00")
        result = self.scheduler.enqueue_manual([rule], at(16, 9))
        run = self.scheduler.tick(at(16, 9))
        self.assertEqual(at(16, 21).isoformat(), run["scheduled_at"])
        self.assertEqual(at(16, 9).isoformat(), run["eligible_at"])
        self.close_run(run)
        self.assertIsNone(self.scheduler.tick(at(16, 21)))
        self.assertEqual(1, len(self.store.list_runs()))
        self.assertEqual(result["accepted"][0]["id"], run["id"])

    def test_manual_existing_due_instance_preserves_identity(self):
        rule = self.rule()
        self.store.set_setting("paused", True)
        self.scheduler.tick(at())
        old = self.store.today_runs(at())[rule]
        result = self.scheduler.enqueue_manual([rule], at(16, 9))
        self.assertEqual(old["id"], result["accepted"][0]["id"])
        self.assertEqual(old["message"], result["accepted"][0]["message"])
        self.assertIsNone(self.scheduler.tick(at(16, 9)))
        self.store.set_setting("paused", False)
        self.assertEqual(old["id"], self.scheduler.tick(at(16, 9))["id"])

    def test_schedule_disabled_and_unset_time_still_allow_manual(self):
        rule = self.rule(enabled=False, time=None)
        self.assertIsNone(self.store.list_rules()[0]["send_time"])
        self.assertIsNone(self.scheduler.tick(at()))
        self.scheduler.enqueue_manual([rule], at())
        run = self.scheduler.tick(at())
        self.assertEqual(rule, run["rule_id"])
        self.close_run(run)
        again = self.scheduler.enqueue_manual([rule], at(16, 10))["accepted"][0]
        self.assertNotEqual(run["id"], again["id"])
        self.assertEqual(0, again["is_daily"])

    def test_disabling_timer_preserves_explicit_manual_queue(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at(16, 7, 30))
        self.store.set_rule_enabled(rule, False)
        self.assertEqual("pending", self.store.today_runs(at())[rule]["status"])
        self.assertIsNotNone(self.scheduler.tick(at()))

    def test_disabling_timer_cancels_timer_only_but_explicit_manual_can_reuse_it(self):
        rule = self.rule()
        self.store.set_setting("paused", True)
        self.scheduler.tick(at())
        old = self.store.today_runs(at())[rule]
        self.store.set_rule_enabled(rule, False)
        self.assertEqual("cancelled", self.store.today_runs(at())[rule]["status"])
        result = self.scheduler.enqueue_manual([rule], at(16, 9))
        self.assertEqual(old["id"], result["accepted"][0]["id"])
        self.store.set_setting("paused", False)
        self.assertEqual(old["id"], self.scheduler.tick(at(16, 9))["id"])

    def test_due_timers_preempt_manual_between_jobs(self):
        manual_a = self.rule(time=None, enabled=False)
        manual_b = self.rule(time=None, enabled=False)
        timed = self.rule(time="08:00")
        self.scheduler.enqueue_manual([manual_a, manual_b], at(16, 7, 59))
        first = self.scheduler.tick(at(16, 7, 59))
        self.assertEqual(manual_a, first["rule_id"])
        self.close_run(first)
        due = self.scheduler.tick(at())
        self.assertEqual(timed, due["rule_id"])
        self.close_run(due)
        self.assertEqual(manual_b, self.scheduler.tick(at())["rule_id"])

    def test_batch_order_is_stable_even_for_same_timestamp_and_conversion(self):
        rules = [self.rule() for _ in range(10)]
        self.store.set_setting("paused", True)
        self.scheduler.tick(at())
        self.scheduler.enqueue_manual(list(reversed(rules)), at(16, 9))
        self.store.set_setting("paused", False)
        actual = []
        for _ in rules:
            run = self.scheduler.tick(at(16, 9))
            actual.append(run["rule_id"])
            self.close_run(run)
        self.assertEqual(list(reversed(rules)), actual)

    def test_manual_batch_order_ignores_original_timer_times(self):
        first = self.rule(time="21:00")
        second = self.rule(time="09:00")
        self.scheduler.enqueue_manual([first, second], at(16, 10))
        run = self.scheduler.tick(at(16, 10))
        self.assertEqual(first, run["rule_id"])
        self.close_run(run)
        self.assertEqual(second, self.scheduler.tick(at(16, 10))["rule_id"])

    def test_pause_login_and_review_gates_both_types(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        for key, closed in (("paused", True), ("account_ready", False), ("login_review_required", True)):
            self.store.set_setting(key, closed)
            self.assertIsNone(self.scheduler.tick(at()))
            self.store.set_setting(key, not closed)
        self.assertIsNotNone(self.scheduler.tick(at()))

    def test_held_rule_does_not_block_another_configuration(self):
        a, b = self.rule(), self.rule()
        self.assertEqual(b, self.scheduler.tick(at(), exclude_rule_ids=(a,))["rule_id"])
        self.assertEqual("pending", self.store.today_runs(at())[a]["status"])

    def test_restart_manual_requires_explicit_resume_even_when_timer_due(self):
        rule = self.rule(time="21:00")
        result = self.scheduler.enqueue_manual([rule], at(16, 9))
        self.restart()
        self.assertIsNone(self.scheduler.tick(at(16, 21)))
        batches = self.scheduler.list_resumable_batches(at(16, 21))
        self.assertEqual(result["batch_id"], batches[0]["id"])
        self.assertEqual(1, self.scheduler.resume_batch(result["batch_id"], at(16, 21)))
        self.assertEqual(result["accepted"][0]["id"], self.scheduler.tick(at(16, 21))["id"])

    def test_interrupted_is_uncertain_while_other_batch_items_wait_resume(self):
        a, b = self.rule(), self.rule()
        batch = self.scheduler.enqueue_manual([a, b], at(16, 7, 30))
        first = self.scheduler.tick(at(16, 7, 30))
        self.restart()
        self.assertEqual("uncertain", self.store.today_runs(at())[a]["status"])
        self.assertEqual("possible", self.store.today_runs(at())[a]["submission"])
        self.scheduler.complete(first["id"], "persisted_in_chat")
        self.assertEqual("uncertain", self.store.today_runs(at())[a]["status"])
        self.assertIsNone(self.scheduler.tick(at()))
        self.scheduler.resume_batch(batch["batch_id"], at())
        self.assertEqual(b, self.scheduler.tick(at())["rule_id"])

    def test_yesterdays_manual_batch_expires_not_resumed(self):
        rule = self.rule(enabled=False)
        batch = self.scheduler.enqueue_manual([rule], at())
        self.restart()
        self.assertEqual([], self.scheduler.list_resumable_batches(at(17)))
        with self.assertRaises(ValueError):
            self.scheduler.resume_batch(batch["batch_id"], at(17))
        self.assertEqual("missed", self.store.today_runs(at())[rule]["status"])
        self.assertIsNone(self.scheduler.tick(at(17)))

    def test_same_day_rollover_records_missed_and_today(self):
        self.rule()
        run = self.scheduler.tick(at(17, 9))
        self.assertEqual(at(17).isoformat(), run["scheduled_at"])
        self.assertEqual("missed", self.store.today_runs(at())[run["rule_id"]]["status"])

    def test_old_all_policy_cannot_send_crossday(self):
        self.rule()
        self.store.set_setting("late_policy", "all")
        run = self.scheduler.tick(at(18, 9))
        self.assertEqual(at(18).date().isoformat(), run["scheduled_date"])
        self.assertEqual(2, len(self.store.list_runs()))

    def test_long_shutdown_has_bounded_catchup(self):
        self.rule()
        future = at().replace(year=2060)
        self.assertEqual(future.date().isoformat(), self.scheduler.tick(future)["scheduled_date"])
        self.assertEqual(2, len(self.store.list_runs()))

    def test_edit_cancels_pending_without_rewriting_daily_snapshot(self):
        rule = self.rule()
        self.store.set_setting("paused", True)
        self.scheduler.tick(at())
        old = self.store.today_runs(at())[rule]
        self.rule(now=at(16, 9), message="新正文", time="10:00", rule_id=rule)
        self.store.set_setting("paused", False)
        self.assertIsNone(self.scheduler.tick(at(16, 10)))
        self.assertEqual("你好", self.store.today_runs(at())[rule]["message"])
        self.assertEqual(old["id"], self.store.today_runs(at())[rule]["id"])
        extra = self.scheduler.enqueue_manual([rule], at(16, 10))["accepted"][0]
        self.assertNotEqual(old["id"], extra["id"])
        self.assertEqual("新正文", extra["message"])
        self.close_run(self.scheduler.tick(at(16, 10)))
        self.assertEqual("你好", self.store.today_rule_states(at())[rule]["daily"]["message"])
        self.assertEqual("新正文", self.scheduler.tick(at(17, 10))["message"])

    def test_manual_queue_keeps_original_payload_after_config_edit(self):
        rule = self.rule(time="21:00")
        original = self.scheduler.enqueue_manual([rule], at(16, 9))["accepted"][0]
        self.rule(now=at(16, 9), message="新正文", time="22:00", rule_id=rule)
        run = self.scheduler.tick(at(16, 10))
        self.assertEqual(original["id"], run["id"])
        self.assertEqual("你好", run["message"])
        self.assertEqual(at(16, 21).isoformat(), run["scheduled_at"])
        self.close_run(run)
        self.assertIsNone(self.scheduler.tick(at(16, 22)))
        self.assertEqual("新正文", self.scheduler.tick(at(17, 22))["message"])

    def test_manual_safe_retry_keeps_original_payload_after_edit(self):
        rule = self.rule(enabled=False, time=None)
        self.scheduler.enqueue_manual([rule], at())
        original = self.scheduler.tick(at())
        self.close_run(original, "failed", submission="not_submitted")
        self.rule(now=at(16, 9), message="新正文", enabled=False, time=None, rule_id=rule)
        retried = self.scheduler.enqueue_manual([rule], at(16, 10))["accepted"][0]
        self.assertEqual(original["id"], retried["id"])
        self.assertEqual("你好", retried["message"])
        run = self.scheduler.tick(at(16, 10))
        self.assertEqual("你好", run["message"])

    def test_manual_binding_edit_cancels_even_with_identical_name(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        self.rule(now=at(16, 9), contact=1, rule_id=rule)
        run = self.store.today_runs(at())[rule]
        self.assertEqual("cancelled", run["status"])
        self.assertEqual("binding_changed", run["code"])
        extra = self.scheduler.enqueue_manual([rule], at(16, 9))["accepted"][0]
        self.assertNotEqual(run["id"], extra["id"])
        self.assertEqual("friend-b", extra["platform_id"])

    def test_manual_defer_after_message_edit_preserves_original_snapshot(self):
        rule = self.rule(enabled=False, time=None)
        self.scheduler.enqueue_manual([rule], at())
        run = self.scheduler.tick(at())
        self.rule(message="新正文", time=None, enabled=False, rule_id=rule)
        self.assertTrue(self.scheduler.defer(run["id"], submission="not_submitted"))
        self.store.set_setting("account_ready", True)
        resumed = self.scheduler.tick(at())
        self.assertEqual(run["id"], resumed["id"])
        self.assertEqual("你好", resumed["message"])

    def test_completed_edit_does_not_send_again_today(self):
        rule = self.rule()
        self.close_run(self.scheduler.tick(at()))
        self.rule(now=at(16, 9), message="新内容", time="10:00", rule_id=rule)
        self.assertIsNone(self.scheduler.tick(at(16, 10)))

    def test_delete_cancels_manual_pending(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        self.store.delete_rule(rule)
        self.assertEqual("cancelled", self.store.today_runs(at())[rule]["status"])
        self.assertIsNone(self.scheduler.tick(at()))

    def test_reenable_does_not_backfill_disabled_time(self):
        rule = self.rule(enabled=False)
        with patch("spark_assistant.store.local_now", return_value=at(18, 9)):
            self.store.set_rule_enabled(rule, True)
        self.assertIsNone(self.scheduler.tick(at(18, 10)))
        self.assertIsNotNone(self.scheduler.tick(at(19)))

    def test_safe_retry_same_run_payload_and_only_explicit(self):
        rule = self.rule()
        first = self.scheduler.tick(at())
        self.close_run(first, "failed", submission="not_submitted", code="friend_blocked")
        self.assertIsNone(self.scheduler.tick(at(16, 9)))
        retry = self.scheduler.retry_run(first["id"], at(16, 9))
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual(first["message"], retry["message"])
        self.assertEqual(first["platform_id"], retry["platform_id"])
        self.assertEqual(rule, self.scheduler.tick(at(16, 9))["rule_id"])

    def test_bulk_explicit_retry_reuses_failed_run_in_new_batch(self):
        rule = self.rule()
        first = self.scheduler.tick(at())
        self.close_run(first, "failed", submission="not_submitted")
        result = self.scheduler.enqueue_manual([rule], at(16, 9))
        self.assertEqual(first["id"], result["accepted"][0]["id"])
        self.assertEqual(result["batch_id"], result["accepted"][0]["batch_id"])

    def test_original_uncertain_unknown_and_confirmed_request_never_retry(self):
        for index, (status, submission) in enumerate((("uncertain", "possible"), ("failed", "unknown"), ("persisted_in_chat", "confirmed"))):
            rule = self.rule(time=None, enabled=False)
            self.scheduler.enqueue_manual([rule], at())
            run = self.scheduler.tick(at())
            self.close_run(run, status, submission=submission)
            with self.assertRaises(ValueError):
                self.scheduler.retry_run(run["id"], at(16, 9))
            again = self.scheduler.enqueue_manual([rule], at(16, 9))
            if submission == "confirmed":
                self.assertNotEqual(run["id"], again["accepted"][0]["id"])
            else:
                self.assertEqual([], again["accepted"])
                self.assertEqual("confirmation_required", again["rejected"][0]["code"])

    def test_changed_payload_safe_retry_keeps_failed_record_original_message(self):
        rule = self.rule()
        first = self.scheduler.tick(at())
        self.close_run(first, "failed", submission="not_submitted")
        self.rule(message="changed", rule_id=rule)
        retry = self.scheduler.retry_run(first["id"], at(16, 9))
        self.assertEqual(first["id"], retry["id"])
        self.assertEqual("你好", retry["message"])

    def test_crossday_safe_retry_rejected(self):
        self.rule()
        first = self.scheduler.tick(at())
        self.close_run(first, "failed", submission="not_submitted")
        with self.assertRaisesRegex(ValueError, "当天"):
            self.scheduler.retry_run(first["id"], at(17))

    def test_defer_preserves_manual_with_schedule_off(self):
        rule = self.rule(enabled=False, time=None)
        self.scheduler.enqueue_manual([rule], at())
        first = self.scheduler.tick(at())
        self.assertTrue(self.scheduler.defer(first["id"], submission="not_submitted", code="login_required"))
        self.assertFalse(self.store.get_setting("account_ready"))
        self.assertIsNone(self.scheduler.tick(at(16, 9)))
        self.store.set_setting("account_ready", True)
        self.assertEqual(first["id"], self.scheduler.tick(at(16, 9))["id"])

    def test_defer_cannot_revive_possible_or_uncertain(self):
        self.rule()
        run = self.scheduler.tick(at())
        self.assertFalse(self.scheduler.defer(run["id"], submission="possible"))
        self.scheduler.recover_interrupted()
        self.assertFalse(self.scheduler.defer(run["id"]))
        self.assertEqual("uncertain", self.store.list_runs()[0]["status"])

    def test_defer_after_edit_or_delete_cancels_original(self):
        rule = self.rule()
        run = self.scheduler.tick(at())
        self.rule(message="new", rule_id=rule)
        self.scheduler.defer(run["id"])
        self.assertEqual("cancelled", self.store.today_runs(at())[rule]["status"])

    def test_deferred_run_expires_after_midnight(self):
        rule = self.rule(enabled=False)
        self.scheduler.enqueue_manual([rule], at())
        run = self.scheduler.tick(at())
        self.scheduler.defer(run["id"])
        self.store.set_setting("account_ready", True)
        self.assertIsNone(self.scheduler.tick(at(17, 7)))
        self.assertEqual("missed", self.store.today_runs(at())[rule]["status"])

    def test_parallel_connections_claim_once(self):
        self.rule()
        self.rule()
        def claim(_):
            store = Store(self.path)
            try:
                return Scheduler(store).tick(at())
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, range(2)))
        self.assertEqual(1, sum(result is not None for result in results))

    def test_concurrent_manual_enqueue_keeps_one_daily_record(self):
        rule = self.rule()
        def enqueue(_):
            store = Store(self.path)
            try:
                return Scheduler(store).enqueue_manual([rule], at())
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, range(2)))
        self.assertEqual(1, sum(len(result["accepted"]) for result in results))
        self.assertEqual(1, len(self.store.list_runs()))

    def test_input_validation_and_timezone(self):
        self.assertEqual(" hello ", Store._validate_rule(" friend ", " hello ", "08:00")[1])
        for message in ("", "hi\nthere", "a" * 501, " \u200b\ufeff "):
            with self.assertRaises(ValueError):
                self.store.save_rule("friend", message, "08:00", True)
        for time in ("24:00", "8:00", "08:60", "08:00:00", None):
            with self.assertRaises(ValueError):
                self.store.save_rule("friend", "hi", time, True)
        self.rule()
        self.assertEqual(at().isoformat(), self.scheduler.tick(at().astimezone(timezone.utc))["scheduled_at"])
        with self.assertRaises(ValueError):
            self.scheduler.tick(datetime(2035, 10, 16, 8))

    def test_today_runs_is_not_limited_to_default_history_limit(self):
        ids = [self.rule(enabled=False, time=None) for _ in range(205)]
        self.scheduler.enqueue_manual(ids, at())
        self.assertEqual(200, len(self.store.list_runs()))
        self.assertEqual(205, len(self.store.today_runs(at())))

    def test_migration_backs_up_and_preserves_ids_without_binding_old_names(self):
        path = Path(self.tmp.name) / "v1.db"
        create_v1(path)
        migrated = Store(path)
        try:
            self.assertIsNotNone(migrated.migration_backup)
            with closing(sqlite3.connect(migrated.migration_backup)) as original:
                self.assertEqual(1, original.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual("running", original.execute("SELECT status FROM runs WHERE id='working'").fetchone()[0])
            rule = migrated.list_rules()[0]
            self.assertEqual("old", rule["id"])
            self.assertTrue(rule["needs_review"])
            self.assertIsNone(rule["contact_id"])
            records = {r["id"]: r for r in migrated.list_runs()}
            self.assertEqual("persisted_in_chat", records["done"]["status"])
            self.assertEqual("uncertain", records["working"]["status"])
            self.assertEqual("cancelled", records["queued"]["status"])
            migrated.apply_friend_sync(directory())
            migrated.set_setting("account_ready", True)
            migrated.set_setting("paused", False)
            self.assertIsNone(Scheduler(migrated).tick(at()))
        finally:
            migrated.close()
        reopened = Store(path)
        try:
            self.assertIsNone(reopened.migration_backup)
        finally:
            reopened.close()

    def test_newer_database_rejected_without_downgrade(self):
        path = Path(self.tmp.name) / "future.db"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA user_version=4")
        with self.assertRaisesRegex(ValueError, "更新版本"):
            Store(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(4, conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual([], conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

    def test_pre_release_v2_missing_route_columns_is_backed_up_and_fail_closed(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        self.store.close()
        with closing(sqlite3.connect(self.path)) as conn:
            for table in ("contacts", "runs"):
                conn.execute(f"ALTER TABLE {table} DROP COLUMN conversation_id")
                conn.execute(f"ALTER TABLE {table} DROP COLUMN conversation_type")
            conn.execute("PRAGMA user_version=2")
            conn.commit()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store)
        self.assertIsNotNone(self.store.migration_backup)
        with closing(sqlite3.connect(self.store.migration_backup)) as original:
            self.assertNotIn("conversation_id", {r[1] for r in original.execute("PRAGMA table_info(contacts)")})
            self.assertEqual(2, original.execute("PRAGMA user_version").fetchone()[0])
        self.assertTrue(all(not c["sendable"] for c in self.store.list_contacts()))
        self.assertIsNone(self.scheduler.tick(at()))
        self.assertIsNone(self.store.today_runs(at())[rule]["conversation_id"])

    def test_explicit_repeat_creates_new_request_but_not_another_daily_slot(self):
        rule = self.rule(time="21:00")
        first = self.scheduler.enqueue_manual([rule], at(16, 9), action_id="first-click")["accepted"][0]
        self.close_run(self.scheduler.tick(at(16, 9)))
        second = self.scheduler.enqueue_manual([rule], at(16, 10), action_id="second-click")["accepted"][0]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual((1, 0), (first["is_daily"], second["is_daily"]))
        self.close_run(self.scheduler.tick(at(16, 10)))
        self.assertIsNone(self.scheduler.tick(at(16, 21)))
        state = self.store.today_rule_states(at())[rule]
        self.assertEqual(first["id"], state["daily"]["id"])
        self.assertEqual(second["id"], state["latest"]["id"])
        self.assertEqual(2, state["count"])
        self.assertIsNone(state["active"])
        self.assertEqual(1, sum(r["is_daily"] for r in self.store.list_runs()))

    def test_repeat_after_completed_timer_uses_current_message(self):
        rule = self.rule()
        daily = self.scheduler.tick(at())
        self.close_run(daily)
        self.rule(now=at(16, 9), message="用户新输入", rule_id=rule)
        repeated = self.scheduler.enqueue_manual([rule], at(16, 9))["accepted"][0]
        self.assertNotEqual(daily["id"], repeated["id"])
        self.assertEqual("用户新输入", repeated["message"])
        self.assertEqual("你好", self.store.today_rule_states(at())[rule]["daily"]["message"])

    def test_same_action_replay_after_completion_does_not_create_message(self):
        rule = self.rule()
        first = self.scheduler.enqueue_manual([rule], at(), action_id="same-action")
        self.close_run(self.scheduler.tick(at()))
        replay = self.scheduler.enqueue_manual([rule], at(16, 10), action_id="same-action")
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["accepted"][0]["id"], replay["accepted"][0]["id"])
        self.assertEqual(1, len(self.store.list_runs()))
        self.assertIsNone(self.scheduler.tick(at(16, 10)))

    def test_action_id_cannot_be_reused_for_other_selection_or_day(self):
        a, b = self.rule(), self.rule()
        self.scheduler.enqueue_manual([a], at(), action_id="bound-action")
        with self.assertRaisesRegex(ValueError, "不同请求"):
            self.scheduler.enqueue_manual([b], at(), action_id="bound-action")
        with self.assertRaisesRegex(ValueError, "不同请求"):
            self.scheduler.enqueue_manual([a], at(17), action_id="bound-action")
        self.assertEqual(1, len(self.store.list_runs()))

    def test_rejected_action_replay_does_not_later_turn_into_a_send(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at(), action_id="active")
        rejected = self.scheduler.enqueue_manual([rule], at(), action_id="duplicate-click")
        self.assertEqual("already_queued", rejected["rejected"][0]["code"])
        self.close_run(self.scheduler.tick(at()))
        replay = self.scheduler.enqueue_manual([rule], at(16, 9), action_id="duplicate-click")
        self.assertTrue(replay["replayed"])
        self.assertEqual([], replay["accepted"])
        self.assertEqual(1, len(self.store.list_runs()))

    def test_concurrent_same_action_is_one_persisted_operation(self):
        rule = self.rule()
        def enqueue(_):
            store = Store(self.path)
            try:
                return Scheduler(store).enqueue_manual([rule], at(), action_id="concurrent-action")
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, range(2)))
        self.assertEqual(1, sum(r["replayed"] for r in results))
        self.assertEqual(results[0]["accepted"][0]["id"], results[1]["accepted"][0]["id"])
        self.assertEqual(1, len(self.store.list_runs()))

    def test_pending_and_running_repeat_clicks_never_add_a_second_job(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        self.assertEqual("already_queued", self.scheduler.enqueue_manual([rule], at())["rejected"][0]["code"])
        self.scheduler.tick(at())
        self.assertEqual("already_queued", self.scheduler.enqueue_manual([rule], at())["rejected"][0]["code"])
        self.assertEqual(1, len(self.store.list_runs()))

    def test_concurrent_distinct_repeat_actions_enqueue_only_one_extra(self):
        rule = self.rule()
        self.close_run(self.scheduler.tick(at()))
        def enqueue(index):
            store = Store(self.path)
            try:
                return Scheduler(store).enqueue_manual([rule], at(16, 9), action_id=f"repeat-click-{index}")
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, range(2)))
        self.assertEqual(1, sum(len(result["accepted"]) for result in results))
        self.assertEqual(2, len(self.store.list_runs()))
        self.assertEqual(1, sum(run["status"] == "pending" for run in self.store.list_runs()))

    def test_exact_unknown_ack_creates_new_request_and_keeps_unknown_original(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        original = self.scheduler.tick(at())
        self.close_run(original, "uncertain", submission="possible")
        default = self.scheduler.enqueue_manual([rule], at(16, 9))
        self.assertEqual("confirmation_required", default["rejected"][0]["code"])
        self.assertEqual(original["id"], default["rejected"][0]["run_id"])
        wrong = self.scheduler.enqueue_manual([rule], at(16, 9), acknowledge_uncertain_ids=("some-other-run",))
        self.assertEqual([], wrong["accepted"])
        confirmed = self.scheduler.enqueue_manual([rule], at(16, 9), action_id="acknowledged-click",
                                                   acknowledge_uncertain_ids=(original["id"],))
        extra = confirmed["accepted"][0]
        self.assertNotEqual(original["id"], extra["id"])
        self.assertEqual(0, extra["is_daily"])
        state = self.store.today_rule_states(at())[rule]
        self.assertEqual("uncertain", state["daily"]["status"])
        self.assertEqual(extra["id"], state["active"]["id"])
        self.assertFalse(state["requires_confirmation"])

    def test_old_ack_cannot_authorize_a_newer_unknown_result(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        first = self.scheduler.tick(at())
        self.close_run(first, "uncertain", submission="possible")
        self.scheduler.enqueue_manual([rule], at(), acknowledge_uncertain_ids=(first["id"],))
        second = self.scheduler.tick(at())
        self.close_run(second, "uncertain", submission="possible")
        stale = self.scheduler.enqueue_manual([rule], at(), acknowledge_uncertain_ids=(first["id"],))
        self.assertEqual([], stale["accepted"])
        self.assertEqual(second["id"], stale["rejected"][0]["run_id"])
        self.assertEqual(2, len(self.store.list_runs()))

    def test_unknown_ack_action_replay_is_idempotent_after_new_message_finishes(self):
        rule = self.rule()
        self.scheduler.enqueue_manual([rule], at())
        original = self.scheduler.tick(at())
        self.close_run(original, "uncertain", submission="possible")
        ack = {"action_id": "ack-once", "acknowledge_uncertain_ids": (original["id"],)}
        result = self.scheduler.enqueue_manual([rule], at(), **ack)
        self.close_run(self.scheduler.tick(at()))
        replay = self.scheduler.enqueue_manual([rule], at(16, 9), **ack)
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["accepted"][0]["id"], replay["accepted"][0]["id"])
        self.assertEqual(2, len(self.store.list_runs()))

    def test_safe_failed_repeat_keeps_request_after_prior_action_replay(self):
        rule = self.rule()
        initial = self.scheduler.enqueue_manual([rule], at(), action_id="failed-initial")
        run = self.scheduler.tick(at())
        self.close_run(run, "failed", submission="not_submitted")
        self.rule(message="新的配置文字", rule_id=rule)
        retried = self.scheduler.enqueue_manual([rule], at(16, 9), action_id="safe-retry")
        self.assertEqual(run["id"], retried["accepted"][0]["id"])
        self.assertEqual("你好", retried["accepted"][0]["message"])
        replay = self.scheduler.enqueue_manual([rule], at(16, 9), action_id="failed-initial")
        self.assertTrue(replay["replayed"])
        self.assertEqual(initial["accepted"][0]["id"], replay["accepted"][0]["id"])
        self.assertEqual(1, len(self.store.list_runs()))

    def test_safe_retry_api_cannot_add_an_active_duplicate(self):
        rule = self.rule()
        original = self.scheduler.tick(at())
        self.close_run(original, "failed", submission="not_submitted")
        self.scheduler.retry_run(original["id"], at())
        with self.assertRaises(ValueError):
            self.scheduler.retry_run(original["id"], at())
        self.assertEqual(1, len(self.store.list_runs()))

    def test_restart_repeat_queue_requires_resume_and_daily_never_reruns(self):
        rule = self.rule(time="21:00")
        self.scheduler.enqueue_manual([rule], at(16, 9))
        self.close_run(self.scheduler.tick(at(16, 9)))
        repeated = self.scheduler.enqueue_manual([rule], at(16, 10))
        self.restart()
        self.assertIsNone(self.scheduler.tick(at(16, 21)))
        self.assertEqual("already_queued", self.scheduler.enqueue_manual([rule], at(16, 21))["rejected"][0]["code"])
        self.scheduler.resume_batch(repeated["batch_id"], at(16, 21))
        self.assertEqual(repeated["accepted"][0]["id"], self.scheduler.tick(at(16, 21))["id"])

    def test_restart_running_repeat_becomes_unknown_without_auto_new_message(self):
        rule = self.rule()
        self.close_run(self.scheduler.tick(at()))
        self.scheduler.enqueue_manual([rule], at(16, 9))
        repeat = self.scheduler.tick(at(16, 9))
        self.restart()
        self.assertIsNone(self.scheduler.tick(at(16, 10)))
        state = self.store.today_rule_states(at())[rule]
        self.assertEqual(repeat["id"], state["latest"]["id"])
        self.assertTrue(state["requires_confirmation"])
        self.assertEqual("confirmation_required", self.scheduler.enqueue_manual([rule], at(16, 10))["rejected"][0]["code"])

    def test_repeat_expires_crossday_and_next_day_has_fresh_daily_slot(self):
        rule = self.rule()
        self.close_run(self.scheduler.tick(at()))
        extra = self.scheduler.enqueue_manual([rule], at(16, 23))["accepted"][0]
        next_day = self.scheduler.tick(at(17))
        self.assertEqual(1, next_day["is_daily"])
        self.assertEqual("2035-10-17", next_day["scheduled_date"])
        previous = {r["id"]: r for r in self.store.list_runs()}
        self.assertEqual("missed", previous[extra["id"]]["status"])

    def test_partial_unique_index_allows_extras_but_only_one_daily_request(self):
        rule = self.rule()
        first = self.scheduler.tick(at())
        self.close_run(first)
        for hour in (9, 10, 11):
            self.scheduler.enqueue_manual([rule], at(16, hour))
            self.close_run(self.scheduler.tick(at(16, hour)))
        self.assertEqual(4, len(self.store.list_runs()))
        self.assertEqual(1, sum(r["is_daily"] for r in self.store.list_runs()))
        self.assertIsNone(self.scheduler.tick(at(16, 12)))
        with self.store.transaction() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE runs SET is_daily=1 WHERE is_daily=0")

    def test_v2_upgrade_preserves_ids_and_allows_repeat_without_rewriting_ledger(self):
        rule = self.rule()
        first = self.scheduler.tick(at())
        self.close_run(first)
        self.store.close()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("DROP INDEX runs_daily_unique")
            conn.execute("ALTER TABLE runs DROP COLUMN is_daily")
            conn.execute("CREATE UNIQUE INDEX v2_full_daily_unique ON runs(rule_id,scheduled_date)")
            conn.execute("ALTER TABLE batches DROP COLUMN action_signature")
            conn.execute("ALTER TABLE batches DROP COLUMN action_result")
            conn.execute("PRAGMA user_version=2")
            conn.commit()
        self.store = Store(self.path)
        self.scheduler = Scheduler(self.store)
        self.assertIsNotNone(self.store.migration_backup)
        with closing(sqlite3.connect(self.store.migration_backup)) as backup:
            self.assertEqual(2, backup.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(first["id"], backup.execute("SELECT id FROM runs").fetchone()[0])
        old = self.store.today_rule_states(at())[rule]["daily"]
        self.assertEqual(first["id"], old["id"])
        self.assertEqual("persisted_in_chat", old["status"])
        self.assertEqual(first["conversation_id"], old["conversation_id"])
        repeated = self.scheduler.enqueue_manual([rule], at(16, 9))["accepted"][0]
        self.assertNotEqual(old["id"], repeated["id"])
        self.assertEqual(0, repeated["is_daily"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
