"""Disposable queue and migration checks executed inside the frozen application."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from .scheduler import Scheduler
from .store import CHINA_TZ, Store


def fixture_directory() -> dict:
    """Synthetic identities only; never invoke a browser with this data."""
    return {
        "account_validated": True,
        "account": {"platform_id": "fixture-owner", "verification": "verified"},
        "friends": [
            {"platform_id": key, "profile_url": f"https://www.douyin.com/user/{key}",
             "display_name": "同名测试好友", "relationship": "existing_chat", "source": "official_chat_ui",
             "identity_verified": True, "dm_verified": True, "conversation_id": "chat-" + key,
             "conversation_type": "one_to_one"}
            for key in ("fixture-friend-a", "fixture-friend-b")
        ],
        "coverage": {"status": "complete", "reason_code": "", "observed_count": 2,
                     "directory_kind": "existing_chats"},
    }


def check_v020_state() -> dict:
    """Exercise migration and dispatch without network or persistent user state."""
    checks = {}

    def require(condition, name):
        if not condition:
            raise AssertionError(name)
        checks[name] = "passed"

    morning = datetime(2035, 1, 2, 9, tzinfo=CHINA_TZ)
    later = morning.replace(hour=10)
    evening = morning.replace(hour=21)
    with tempfile.TemporaryDirectory(prefix="spark-v020-check-") as folder:
        store = Store(Path(folder) / "fresh.db")
        try:
            store.apply_friend_sync(fixture_directory(), account_epoch=store.get_account()["account_epoch"])
            contacts = store.list_contacts()
            require(len(contacts) == 2 and contacts[0]["id"] != contacts[1]["id"], "same_names_keep_identity")
            store.set_setting("account_ready", True)
            with patch("spark_assistant.store.local_now", return_value=morning):
                timed = store.save_rule("同名测试好友", "offline fixture A", "21:00", True,
                                        contact_id=contacts[0]["id"])
                manual = store.save_rule("同名测试好友", "offline fixture B", None, False,
                                         contact_id=contacts[1]["id"])
            scheduler = Scheduler(store)
            scheduler.enqueue_manual([timed, manual], now=later)
            scheduler.enqueue_manual([timed], now=later)
            require(len(store.list_runs()) == 2, "manual_double_click_single_occurrence")
            first = scheduler.tick(later)
            require(first is not None and first["rule_id"] == timed, "manual_batch_order")
            scheduler.complete(first["id"], "uncertain", "offline fixture", submission="possible")
            second = scheduler.tick(later)
            require(second is not None and second["rule_id"] == manual, "manual_with_schedule_disabled")
            scheduler.complete(second["id"], "persisted_in_chat", "offline fixture", submission="confirmed")
            require(scheduler.tick(evening) is None and len(store.list_runs()) == 2,
                    "manual_consumes_scheduled_occurrence")
            repeated = scheduler.enqueue_manual([timed, manual], now=evening, action_id="fixture-repeat")
            require(len(repeated["accepted"]) == 1
                    and any(item["code"] == "confirmation_required" for item in repeated["rejected"]),
                    "uncertain_requires_explicit_ack")
            extra = scheduler.tick(evening)
            require(extra is not None and extra["rule_id"] == manual and extra["id"] != second["id"]
                    and not extra["is_daily"], "explicit_manual_repeat_is_new_request")
            scheduler.complete(extra["id"], "persisted_in_chat", "offline fixture", submission="confirmed")
            count_before_replay = len(store.list_runs())
            scheduler.enqueue_manual([timed, manual], now=evening, action_id="fixture-repeat")
            require(len(store.list_runs()) == count_before_replay and scheduler.tick(evening) is None,
                    "same_action_replay_never_resends")
            acknowledged = scheduler.enqueue_manual([timed], now=evening, action_id="fixture-ack",
                                                    acknowledge_uncertain_ids=[first["id"]])
            fresh = scheduler.tick(evening)
            require(len(acknowledged["accepted"]) == 1 and fresh is not None and fresh["id"] != first["id"],
                    "acknowledged_repeat_preserves_original_unknown")
            scheduler.complete(fresh["id"], "uncertain", "offline fixture", submission="possible")
            require(scheduler.tick(evening) is None, "no_automatic_resend_of_unknown")
            with patch("spark_assistant.store.local_now", return_value=morning):
                third = store.save_rule("同名测试好友", "offline fixture C", None, False,
                                        contact_id=contacts[0]["id"])
            queued = scheduler.enqueue_manual([third], now=later)
            scheduler.recover_interrupted()
            require(scheduler.tick(later) is None and bool(scheduler.list_resumable_batches(now=later)),
                    "restart_requires_batch_resume")
            scheduler.resume_batch(queued["batch_id"], now=later)
            resumed = scheduler.tick(later)
            require(resumed is not None and resumed["rule_id"] == third, "explicit_batch_resume")
            scheduler.complete(resumed["id"], "uncertain", "offline fixture", submission="possible")
            old_epoch = store.get_account()["account_epoch"]
            store.invalidate_account_epoch("offline account switch")
            require(store.get_account()["account_epoch"] != old_epoch
                    and not store.get_setting("account_ready"), "account_switch_invalidates_authority")
        finally:
            store.close()

        legacy_path = Path(folder) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript("""
            CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE contacts(name TEXT PRIMARY KEY,position INTEGER NOT NULL);
            CREATE TABLE rules(id TEXT PRIMARY KEY,recipient TEXT NOT NULL,message TEXT NOT NULL,
                send_time TEXT NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,effective_from TEXT NOT NULL,next_scheduled_at TEXT NOT NULL);
            CREATE TABLE runs(id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,recipient TEXT NOT NULL,
                message TEXT NOT NULL,scheduled_at TEXT NOT NULL,scheduled_date TEXT NOT NULL,
                started_at TEXT,finished_at TEXT,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',
                UNIQUE(rule_id,scheduled_date));
            INSERT INTO contacts VALUES('旧备注',0);
            PRAGMA user_version=1;
        """)
        connection.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?)", (
            "legacy-rule", "旧备注", "old fixture", "21:00", 1, *([morning.isoformat()] * 4)))
        connection.commit()
        connection.close()
        migrated = Store(legacy_path)
        try:
            backup = migrated.migration_backup
            require(backup is not None and Path(backup).is_file(), "migration_backup_exists")
            with closing(sqlite3.connect(backup)) as original:
                require(original.execute("PRAGMA user_version").fetchone()[0] == 1
                        and original.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 1,
                        "migration_backup_preserves_v1")
            legacy = migrated.list_rules()[0]
            require(legacy["id"] == "legacy-rule" and not legacy.get("contact_id"),
                    "legacy_names_require_identity_binding")
            migrated.set_setting("account_ready", True)
            require(Scheduler(migrated).tick(evening) is None, "legacy_name_cannot_dispatch")
        finally:
            migrated.close()
    return checks
