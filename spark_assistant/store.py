"""Local state and verified friend bindings; browser credentials live elsewhere."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4

from .validation import normalize_recipient, validate_message

CHINA_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
SCHEMA_VERSION = 3
# The mutual source remains readable for pre-release fixtures. Production uses
# the existing one-to-one chat directory and makes no claim about mutual follows.
CONTACT_SOURCES = {"official_chat_ui": "existing_chat", "official_mutual_ui": "mutual"}


def local_now() -> datetime:
    return datetime.now(CHINA_TZ)


def as_local(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(CHINA_TZ)


def next_occurrence(send_time: str, after: datetime) -> datetime:
    after = as_local(after)
    hour, minute = map(int, send_time.split(":"))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate + timedelta(days=1) if candidate < after else candidate


class Store:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migration_backup: Path | None = None
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._conn.close()
            raise ValueError("数据库由更新版本的软件创建，请使用相同或更新版本打开")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        try:
            if version == 1:
                self._backup_database(1)
                self._migrate_v1()
                self._migrate_v3()
            elif version == 0:
                self._create_schema()
            elif version == 2:
                self._backup_database(2)
                self._ensure_conversation_fields()
                self._migrate_v3()
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
        except BaseException:
            self._conn.close()
            raise

    def _backup_database(self, version):
        suffix = local_now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        backup = self.path.with_name(self.path.name + f".v{version}-backup-" + suffix + ".sqlite3")
        target = sqlite3.connect(backup)
        try:
            self._conn.backup(target)
        finally:
            target.close()
        self.migration_backup = backup

    @staticmethod
    def _v2_tables(conn):
        conn.execute("""CREATE TABLE contacts(
            id TEXT PRIMARY KEY,account_id TEXT NOT NULL,platform_id TEXT NOT NULL,
            profile_url TEXT NOT NULL DEFAULT '',display_name TEXT NOT NULL,
            relationship TEXT NOT NULL,sync_state TEXT NOT NULL,source TEXT NOT NULL,
            verified_at TEXT,account_epoch INTEGER NOT NULL,position INTEGER NOT NULL,
            identity_verified INTEGER NOT NULL DEFAULT 0,dm_verified INTEGER NOT NULL DEFAULT 0,
            blocked_reason TEXT NOT NULL DEFAULT '',conversation_id TEXT,
            conversation_type TEXT NOT NULL DEFAULT 'unknown',
            UNIQUE(account_id,platform_id))""")
        conn.execute("""CREATE TABLE batches(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,
            scheduled_date TEXT NOT NULL,account_id TEXT NOT NULL,account_epoch INTEGER NOT NULL,
            status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',
            action_signature TEXT NOT NULL DEFAULT '',action_result TEXT NOT NULL DEFAULT '')""")
        conn.execute("CREATE INDEX IF NOT EXISTS runs_status_schedule ON runs(status,scheduled_at)")
        conn.execute("CREATE INDEX runs_batch ON runs(batch_id,status)")

    @staticmethod
    def _create_runs(conn, table="runs"):
        conn.execute(f"""CREATE TABLE {table}(id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,recipient TEXT NOT NULL,
            message TEXT NOT NULL,scheduled_at TEXT NOT NULL,scheduled_date TEXT NOT NULL,started_at TEXT,
            finished_at TEXT,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',contact_id TEXT,
            platform_id TEXT,profile_url TEXT,account_id TEXT,account_epoch INTEGER NOT NULL DEFAULT 0,
            trigger TEXT NOT NULL DEFAULT 'scheduled',batch_id TEXT,eligible_at TEXT NOT NULL,
            submission TEXT NOT NULL DEFAULT 'not_submitted',code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,queue_order INTEGER NOT NULL DEFAULT 0,conversation_id TEXT,
            conversation_type TEXT NOT NULL DEFAULT 'unknown',is_daily INTEGER NOT NULL DEFAULT 1 CHECK(is_daily IN (0,1)))""")

    def _create_schema(self):
        with self.transaction() as conn:
            conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("""CREATE TABLE rules(id TEXT PRIMARY KEY,recipient TEXT NOT NULL,message TEXT NOT NULL,
                send_time TEXT NOT NULL,enabled INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                effective_from TEXT NOT NULL,next_scheduled_at TEXT NOT NULL,contact_id TEXT,account_id TEXT,
                account_epoch INTEGER NOT NULL DEFAULT 0,needs_review INTEGER NOT NULL DEFAULT 1)""")
            self._create_runs(conn)
            self._v2_tables(conn)
            conn.execute("CREATE UNIQUE INDEX runs_daily_unique ON runs(rule_id,scheduled_date) WHERE is_daily=1")
            conn.execute("PRAGMA user_version = 3")

    def _migrate_v1(self):
        now = local_now().isoformat()
        with self.transaction() as conn:
            conn.execute("ALTER TABLE contacts RENAME TO contacts_v1")
            for column in ("contact_id TEXT", "account_id TEXT", "account_epoch INTEGER NOT NULL DEFAULT 0",
                           "needs_review INTEGER NOT NULL DEFAULT 1"):
                conn.execute("ALTER TABLE rules ADD COLUMN " + column)
            for column in ("contact_id TEXT", "platform_id TEXT", "profile_url TEXT", "account_id TEXT",
                           "account_epoch INTEGER NOT NULL DEFAULT 0", "trigger TEXT NOT NULL DEFAULT 'scheduled'",
                           "batch_id TEXT", "eligible_at TEXT NOT NULL DEFAULT ''", "submission TEXT NOT NULL DEFAULT 'unknown'",
                           "code TEXT NOT NULL DEFAULT ''", "created_at TEXT NOT NULL DEFAULT ''",
                           "queue_order INTEGER NOT NULL DEFAULT 0", "conversation_id TEXT",
                           "conversation_type TEXT NOT NULL DEFAULT 'unknown'"):
                conn.execute("ALTER TABLE runs ADD COLUMN " + column)
            conn.execute("UPDATE runs SET eligible_at=scheduled_at,created_at=scheduled_at,queue_order=rowid")
            conn.execute("UPDATE runs SET status='uncertain',submission='possible',finished_at=?,code='migration_interrupted',"
                         "detail='旧版执行被中断，结果待核对，不自动重试' WHERE status='running'", (now,))
            conn.execute("UPDATE runs SET status='cancelled',submission='not_submitted',finished_at=?,code='migration_review',"
                         "detail='升级后需重新选择已读取并验证身份的聊天好友，旧排队任务已取消' WHERE status='pending'", (now,))
            self._v2_tables(conn)
            for key, value in {"account_ready": False, "account_verified": False, "account_epoch": 1,
                               "account_platform_id": None, "login_review_required": True,
                               "paused": True, "late_policy": "same_day"}.items():
                self._put_setting(conn, key, value)
            conn.execute("PRAGMA user_version = 2")

    def _ensure_conversation_fields(self):
        """Accept pre-release v2 databases without guessing their old chat routes."""
        missing = []
        for table in ("contacts", "runs"):
            columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in (("conversation_id", "conversation_id TEXT"),
                                     ("conversation_type", "conversation_type TEXT NOT NULL DEFAULT 'unknown'")):
                if name not in columns:
                    missing.append((table, definition))
        if not missing:
            return
        if self.migration_backup is None:
            self._backup_database(2)
        with self.transaction() as conn:
            for table, definition in missing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate_v3(self):
        """Remove the full daily uniqueness while preserving every ledger request ID."""
        with self.transaction() as conn:
            self._create_runs(conn, "runs_v3")
            columns = [row[1] for row in conn.execute("PRAGMA table_info(runs)") if row[1] != "is_daily"]
            names = ",".join(columns)
            conn.execute(f"INSERT INTO runs_v3({names},is_daily) SELECT {names},1 FROM runs")
            conn.execute("DROP TABLE runs")
            conn.execute("ALTER TABLE runs_v3 RENAME TO runs")
            conn.execute("CREATE UNIQUE INDEX runs_daily_unique ON runs(rule_id,scheduled_date) WHERE is_daily=1")
            conn.execute("CREATE INDEX runs_status_schedule ON runs(status,scheduled_at)")
            conn.execute("CREATE INDEX runs_batch ON runs(batch_id,status)")
            batch_columns = {row[1] for row in conn.execute("PRAGMA table_info(batches)")}
            for name in ("action_signature", "action_result"):
                if name not in batch_columns:
                    conn.execute(f"ALTER TABLE batches ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            conn.execute("PRAGMA user_version = 3")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._conn.close()

    @staticmethod
    def _setting(conn, key, default=None):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put_setting(conn, key, value):
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, encoded))

    def get_setting(self, key, default=None):
        with self._lock:
            return self._setting(self._conn, key, default)

    def set_setting(self, key, value):
        with self.transaction() as conn:
            self._put_setting(conn, key, value)

    @classmethod
    def _account(cls, conn):
        return {"platform_id": cls._setting(conn, "account_platform_id"),
                "account_epoch": cls._setting(conn, "account_epoch", 1),
                "verified": bool(cls._setting(conn, "account_verified", False))}

    def get_account(self):
        with self._lock:
            return self._account(self._conn)

    @classmethod
    def _invalidate_account(cls, conn, detail, now):
        epoch = int(cls._account(conn)["account_epoch"]) + 1
        has_rules = bool(conn.execute("SELECT 1 FROM rules LIMIT 1").fetchone())
        for key, value in {"account_epoch": epoch, "account_ready": False,
                           "account_verified": False, "login_review_required": has_rules}.items():
            cls._put_setting(conn, key, value)
        conn.execute("UPDATE contacts SET sync_state='stale'")
        conn.execute("UPDATE rules SET needs_review=1")
        conn.execute("UPDATE runs SET status='cancelled',finished_at=?,code='account_invalidated',detail=? WHERE status='pending'", (now, detail))
        conn.execute("UPDATE batches SET status='cancelled',detail=? WHERE status IN ('queued','resume_required')", (detail,))
        return epoch

    def invalidate_account_epoch(self, detail="账号状态已变更，请重新核验并检查好友绑定"):
        with self.transaction() as conn:
            return self._invalidate_account(conn, str(detail), local_now().isoformat())

    @staticmethod
    def _stable_id(value):
        return value.strip() if isinstance(value, str) and value.strip() and len(value) <= 500 else None

    @classmethod
    def _contact_state(cls, contact, account):
        reason = ""
        if not account["verified"] or not account["platform_id"]:
            reason = "当前抖音账号身份尚未核验"
        elif contact["account_id"] != account["platform_id"] or contact["account_epoch"] != account["account_epoch"]:
            reason = "好友属于其他账号或旧登录会话，请重新同步"
        elif CONTACT_SOURCES.get(contact["source"]) != contact["relationship"]:
            reason = "尚未验证为可用的一对一聊天好友"
        elif contact["sync_state"] != "verified":
            reason = "该好友尚未在本次聊天列表中读取并验证，请先在抖音手动发一条消息后刷新"
        elif not contact["platform_id"] or not contact["identity_verified"]:
            reason = contact["blocked_reason"] or "缺少稳定好友身份，无法安全发送"
        elif contact["source"] == "official_chat_ui" and (not contact["conversation_id"] or contact["conversation_type"] != "one_to_one"):
            reason = contact["blocked_reason"] or "缺少已核验的一对一会话标识，请重新读取聊天列表"
        elif not contact["dm_verified"]:
            reason = contact["blocked_reason"] or "尚未验证该好友的私信路由，暂不可发送"
        return dict(contact, identity_verified=bool(contact["identity_verified"]),
                    dm_verified=bool(contact["dm_verified"]), sendable=not reason, blocked_reason=reason)

    def apply_friend_sync(self, result, *, account_epoch=None):
        """Only observed one-to-one identities can authorize a friend binding.

        For official_chat_ui, dm_verified proves an existing private-conversation
        route, not permission to send: the worker must recheck before submission.
        """
        if not isinstance(result, dict):
            raise ValueError("好友同步结果格式无效")
        account_result, coverage = result.get("account") or {}, result.get("coverage") or {}
        if not isinstance(account_result, dict) or not isinstance(coverage, dict):
            raise ValueError("好友同步身份或完整性信息无效")
        completeness = coverage.get("status", "failed")
        if completeness not in {"complete", "partial", "unsupported", "verification_required", "failed"}:
            completeness = "failed"
        directory_kind = coverage.get("directory_kind", "mutual_friends")
        if directory_kind not in {"existing_chats", "mutual_friends"}:
            completeness = "failed"
        platform_id = self._stable_id(account_result.get("platform_id"))
        verified = result.get("account_validated") is True and account_result.get("verification") == "verified" and bool(platform_id)
        stamp = local_now().isoformat()
        with self.transaction() as conn:
            current = self._account(conn)
            if account_epoch is not None and account_epoch != current["account_epoch"]:
                raise ValueError("该好友同步结果属于旧登录会话，已忽略")
            changed = bool(verified and current["platform_id"] and current["platform_id"] != platform_id)
            if changed:
                self._invalidate_account(conn, "检测到抖音账号变化，旧任务需要重新选择已读取的聊天好友", stamp)
                current = self._account(conn)
            epoch = current["account_epoch"]
            conn.execute("UPDATE contacts SET sync_state='stale' WHERE account_epoch=?", (epoch,))
            if verified:
                self._put_setting(conn, "account_platform_id", platform_id)
                self._put_setting(conn, "account_verified", True)
            else:
                self._put_setting(conn, "account_verified", False)
                self._put_setting(conn, "account_ready", False)
            observed, snapshots, conflicted = set(), {}, set()
            invalid_count = 0
            def disqualify(friend_id):
                if friend_id:
                    conflicted.add(friend_id)
                if friend_id in observed:
                    observed.remove(friend_id)
                    conn.execute("UPDATE contacts SET sync_state='stale' WHERE account_id=? AND platform_id=?", (platform_id, friend_id))

            if verified and completeness in {"complete", "partial"}:
                friends = result.get("friends")
                if not isinstance(friends, list):
                    invalid_count += 1
                for position, friend in enumerate(friends if isinstance(friends, list) else []):
                    if not isinstance(friend, dict):
                        invalid_count += 1
                        continue
                    friend_id = self._stable_id(friend.get("platform_id"))
                    try:
                        name = normalize_recipient(friend.get("display_name"))
                    except (ValueError, TypeError):
                        invalid_count += 1
                        disqualify(friend_id)
                        continue
                    source = friend.get("source")
                    source_matches_directory = source == ("official_chat_ui" if directory_kind == "existing_chats" else "official_mutual_ui")
                    group_or_unknown = (friend.get("is_group") is True
                                        or friend.get("conversation_type") not in (None, "single", "private", "one_to_one", 1)
                                        or friend.get("conversation_kind") not in (None, "single", "private", "one_to_one"))
                    if (not friend_id or not source_matches_directory
                            or CONTACT_SOURCES.get(source) != friend.get("relationship") or group_or_unknown):
                        invalid_count += 1
                        disqualify(friend_id)
                        continue
                    if friend_id in conflicted:
                        continue
                    signature = (name, friend.get("profile_url"), source, friend.get("relationship"),
                                 friend.get("identity_verified"), friend.get("dm_verified"),
                                 friend.get("conversation_id"), friend.get("conversation_type"))
                    if friend_id in observed:
                        if snapshots[friend_id] != signature:
                            invalid_count += 1
                            disqualify(friend_id)
                        continue
                    snapshots[friend_id] = signature
                    observed.add(friend_id)
                    if friend.get("identity_verified") is not True:
                        invalid_count += 1
                    conversation_id = self._stable_id(friend.get("conversation_id"))
                    conversation_type = friend.get("conversation_type", "unknown")
                    if conversation_type not in ("one_to_one", "group", "unknown"):
                        conversation_type = "unknown"
                    route_verified = source != "official_chat_ui" or (bool(conversation_id) and conversation_type == "one_to_one")
                    if not route_verified:
                        invalid_count += 1
                    url = friend.get("profile_url", "")
                    blocked_reason = friend.get("blocked_reason") or friend.get("dm_reason") or ""
                    blocked_reason = blocked_reason[:1000] if isinstance(blocked_reason, str) else ""
                    conn.execute("""INSERT INTO contacts(id,account_id,platform_id,profile_url,display_name,
                        relationship,sync_state,source,verified_at,account_epoch,position,identity_verified,dm_verified,blocked_reason,
                        conversation_id,conversation_type)
                        VALUES(?,?,?,?,?,?,'verified',?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,platform_id) DO UPDATE SET
                        profile_url=excluded.profile_url,display_name=excluded.display_name,relationship=excluded.relationship,
                        sync_state=excluded.sync_state,source=excluded.source,verified_at=excluded.verified_at,
                        account_epoch=excluded.account_epoch,position=excluded.position,
                        identity_verified=excluded.identity_verified,dm_verified=excluded.dm_verified,blocked_reason=excluded.blocked_reason,
                        conversation_id=excluded.conversation_id,conversation_type=excluded.conversation_type""", (
                        str(uuid4()), platform_id, friend_id, url if isinstance(url, str) else "", name, friend["relationship"],
                        friend["source"], stamp, epoch, position,
                        int(friend.get("identity_verified") is True), int(friend.get("dm_verified") is True and route_verified),
                        blocked_reason, conversation_id, conversation_type))
                    # A pending occurrence is bound to the conversation observed at
                    # enqueue time; synchronization cannot retarget it to a new one.
                    conn.execute("UPDATE runs SET status='cancelled',finished_at=?,code='conversation_changed',"
                                 "detail='聊天会话标识已变化，原排队任务已停止，请核对后重新配置' "
                                 "WHERE account_id=? AND platform_id=? AND status='pending' "
                                 "AND conversation_id IS NOT ?", (stamp, platform_id, friend_id, conversation_id))
                reported = coverage.get("reported_total")
                if isinstance(reported, int) and not isinstance(reported, bool) and reported > len(observed):
                    invalid_count += reported - len(observed)
                if invalid_count and completeness == "complete":
                    completeness = "partial"
                if completeness == "complete":
                    conn.execute("UPDATE contacts SET sync_state='missing' WHERE account_id=? AND sync_state='stale'", (platform_id,))
            detail = str(result.get("detail") or coverage.get("detail") or coverage.get("reason_code") or "")
            if not verified:
                detail = detail or "未能验证当前账号身份，好友暂不可发送"
            if invalid_count:
                detail = (detail + "；" if detail else "") + f"有 {invalid_count} 项身份或完整性未核验，不能宣称完整同步"
            sendable_count = conn.execute("SELECT COUNT(*) FROM contacts WHERE account_id=? AND account_epoch=? "
                                         "AND sync_state='verified' AND identity_verified=1 AND dm_verified=1",
                                         (platform_id, epoch)).fetchone()[0] if verified else 0
            metadata = {"completeness": completeness, "detail": detail, "last_sync": stamp,
                        "count": len(observed), "sendable_count": sendable_count,
                        "reported_total": coverage.get("reported_total"), "reason_code": str(coverage.get("reason_code", "")),
                        "account_epoch": epoch, "account_changed": changed, "unverified_count": invalid_count,
                        "directory_kind": directory_kind}
            self._put_setting(conn, "friend_sync", metadata)
            return metadata

    def get_friend_sync(self):
        metadata = self.get_setting("friend_sync", {"completeness": "not_synced", "detail": "尚未读取聊天列表",
                                                    "last_sync": None, "count": 0, "sendable_count": 0,
                                                    "directory_kind": "existing_chats"})
        metadata["sendable_count"] = sum(contact["sendable"] for contact in self.list_contacts())
        return metadata

    def list_contacts(self):
        with self._lock:
            account = self._account(self._conn)
            rows = self._conn.execute("SELECT * FROM contacts WHERE account_id=? ORDER BY position,id", (account["platform_id"],)).fetchall()
            return [self._contact_state(dict(row), account) for row in rows]

    def replace_contacts(self, contacts):
        """Old name-only check results never become verified friends."""
        if not contacts:
            with self.transaction() as conn:
                conn.execute("UPDATE contacts SET sync_state='stale'")

    @classmethod
    def _rule_state(cls, conn, rule):
        rule = dict(rule, enabled=bool(rule["enabled"]), needs_review=bool(rule["needs_review"]), send_time=rule["send_time"] or None)
        account = cls._account(conn)
        contact = conn.execute("SELECT * FROM contacts WHERE id=?", (rule["contact_id"],)).fetchone()
        if rule["needs_review"] or not rule["contact_id"]:
            reason = "请选择已读取并验证身份的聊天好友；未读到时请先在抖音手动发一条消息后刷新"
        elif rule["account_epoch"] != account["account_epoch"] or rule["account_id"] != account["platform_id"]:
            reason = "该任务属于旧账号会话，请重新绑定好友"
        elif contact is None:
            reason = "绑定的好友已不存在"
        else:
            reason = cls._contact_state(dict(contact), account)["blocked_reason"]
        rule.update(sendable=not reason, blocked_reason=reason,
                    display_name=contact["display_name"] if contact else rule["recipient"],
                    platform_id=contact["platform_id"] if contact else None, profile_url=contact["profile_url"] if contact else None,
                    conversation_id=contact["conversation_id"] if contact else None,
                    conversation_type=contact["conversation_type"] if contact else "unknown")
        return rule

    def list_rules(self):
        with self._lock:
            return [self._rule_state(self._conn, dict(row)) for row in self._conn.execute("SELECT * FROM rules ORDER BY created_at,id").fetchall()]

    @staticmethod
    def _validate_rule(recipient, message, send_time, enabled=True):
        recipient, message = normalize_recipient(recipient), validate_message(message)
        if send_time in (None, "") and not enabled:
            return recipient, message, ""
        if not isinstance(send_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
            raise ValueError("发送时间应为 HH:MM，例如 08:30")
        return recipient, message, send_time

    @staticmethod
    def _cancel_pending(conn, rule_id, now, detail, code="rule_changed", *, scheduled_only=False):
        clause = " AND trigger='scheduled'" if scheduled_only else ""
        conn.execute("UPDATE runs SET status='cancelled',finished_at=?,detail=?,code=? WHERE rule_id=? AND status='pending'" + clause,
                     (now, detail, code, rule_id))

    def save_rule(self, recipient, message, send_time=None, enabled=False, rule_id=None, *, contact_id=None):
        recipient, message, send_time = self._validate_rule(recipient, message, send_time, enabled)
        now = local_now()
        stamp = now.isoformat()
        upcoming = next_occurrence(send_time, now).isoformat() if send_time else ""
        with self.transaction() as conn:
            current = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone() if rule_id else None
            if rule_id and current is None:
                raise ValueError("任务已不存在")
            if contact_id is None and current is not None and recipient == current["recipient"] and not current["needs_review"]:
                contact_id = current["contact_id"]
            account = self._account(conn)
            needs_review, account_id, epoch = True, None, 0
            if contact_id:
                contact = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
                if contact is None:
                    raise ValueError("所选好友已不存在，请重新同步")
                state = self._contact_state(dict(contact), account)
                if not state["sendable"]:
                    raise ValueError(state["blocked_reason"])
                recipient = contact["display_name"]
                needs_review, account_id, epoch = False, account["platform_id"], account["account_epoch"]
            if current is None:
                rule_id = str(uuid4())
                conn.execute("""INSERT INTO rules(id,recipient,message,send_time,enabled,created_at,updated_at,
                    effective_from,next_scheduled_at,contact_id,account_id,account_epoch,needs_review)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (rule_id, recipient, message, send_time, int(enabled), stamp,
                    stamp, stamp, upcoming, contact_id, account_id, epoch, int(needs_review)))
            else:
                identity_changed = (current["contact_id"], current["account_id"], current["account_epoch"]) != (contact_id, account_id, epoch)
                payload_changed = (current["recipient"], current["message"], current["send_time"], current["contact_id"], current["account_epoch"]) != (
                    recipient, message, send_time, contact_id, epoch)
                schedule_changed = bool(current["enabled"]) != bool(enabled)
                if identity_changed:
                    self._cancel_pending(conn, rule_id, stamp, "好友身份或账号绑定已修改，原排队任务已取消", "binding_changed")
                elif payload_changed:
                    # A manual click authorized its immutable submitted snapshot.
                    # Edits affect future timer occurrences, not that manual batch.
                    self._cancel_pending(conn, rule_id, stamp, "定时配置已修改，原定时排队实例已取消", scheduled_only=True)
                elif schedule_changed and not enabled:
                    self._cancel_pending(conn, rule_id, stamp, "定时已关闭", "schedule_disabled", scheduled_only=True)
                changed = payload_changed or schedule_changed
                conn.execute("""UPDATE rules SET recipient=?,message=?,send_time=?,enabled=?,updated_at=?,effective_from=?,
                    next_scheduled_at=?,contact_id=?,account_id=?,account_epoch=?,needs_review=? WHERE id=?""", (
                    recipient, message, send_time, int(enabled), stamp, stamp if changed else current["effective_from"],
                    upcoming if changed else current["next_scheduled_at"], contact_id, account_id, epoch, int(needs_review), rule_id))
        return rule_id

    def delete_rule(self, rule_id):
        with self.transaction() as conn:
            self._cancel_pending(conn, rule_id, local_now().isoformat(), "任务已删除", "rule_deleted")
            conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))

    def set_rule_enabled(self, rule_id, enabled):
        now = local_now()
        with self.transaction() as conn:
            current = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
            if current is None:
                raise ValueError("任务已不存在")
            if bool(current["enabled"]) == bool(enabled):
                return
            if enabled and not current["send_time"]:
                raise ValueError("请先设置定时发送时间")
            if not enabled:
                self._cancel_pending(conn, rule_id, now.isoformat(), "定时已关闭", "schedule_disabled", scheduled_only=True)
            conn.execute("UPDATE rules SET enabled=?,updated_at=?,effective_from=?,next_scheduled_at=? WHERE id=?", (
                int(enabled), now.isoformat(), now.isoformat(), next_occurrence(current["send_time"], now).isoformat() if current["send_time"] else "", rule_id))

    def list_runs(self, limit=200):
        with self._lock:
            return [dict(row) for row in self._conn.execute("SELECT * FROM runs ORDER BY scheduled_date DESC,created_at DESC,id LIMIT ?", (max(0, int(limit)),))]

    def today_runs(self, now=None):
        """Latest attempt per configuration, including explicitly repeated messages."""
        date = as_local(now if now is not None else local_now()).date().isoformat()
        with self._lock:
            return {row["rule_id"]: dict(row) for row in self._conn.execute(
                "SELECT * FROM runs WHERE scheduled_date=? ORDER BY queue_order,created_at,id", (date,))}

    def today_rule_states(self, now=None):
        """Expose the daily slot independently from the latest and active requests."""
        date = as_local(now if now is not None else local_now()).date().isoformat()
        with self._lock:
            states = {row[0]: {"daily": None, "active": None, "latest": None,
                               "count": 0, "requires_confirmation": False}
                      for row in self._conn.execute("SELECT id FROM rules")}
            rows = self._conn.execute("SELECT * FROM runs WHERE scheduled_date=? OR status IN ('pending','running') "
                                      "ORDER BY queue_order,created_at,id", (date,)).fetchall()
            for row in rows:
                run = dict(row)
                state = states.setdefault(run["rule_id"], {"daily": None, "active": None, "latest": None,
                                                          "count": 0, "requires_confirmation": False})
                if run["status"] in {"pending", "running"}:
                    state["active"] = run
                if run["scheduled_date"] == date:
                    state["count"] += 1
                    state["latest"] = run
                    if run["is_daily"]:
                        state["daily"] = run
            for state in states.values():
                latest = state["latest"]
                state["requires_confirmation"] = bool(latest and
                    (latest["status"] == "uncertain" or latest["submission"] in {"possible", "unknown", "submitted"}))
            return states
