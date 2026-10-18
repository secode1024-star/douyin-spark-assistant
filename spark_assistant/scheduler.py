"""Daily timer slots and explicit manual requests share one persistent serial queue."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re
from uuid import uuid4

from .store import Store, as_local, local_now

LATE_POLICIES = {"same_day"}


class Scheduler:
    def __init__(self, store: Store):
        self.store = store

    _setting = staticmethod(Store._setting)

    @staticmethod
    def _next_order(conn):
        return conn.execute("SELECT COALESCE(MAX(queue_order),0)+1 FROM runs").fetchone()[0]

    @staticmethod
    def _is_expired(scheduled, now, policy="same_day"):
        return scheduled.date() < now.date()

    @staticmethod
    def _identity_matches(rule, run):
        return (rule["contact_id"] == run["contact_id"] and rule["account_id"] == run["account_id"]
                and rule["account_epoch"] == run["account_epoch"]
                and rule.get("platform_id") == run["platform_id"]
                and rule.get("conversation_id") == run["conversation_id"])

    @classmethod
    def _snapshot_matches(cls, rule, run):
        return cls._identity_matches(rule, run) and (run["trigger"] == "manual" or rule["message"] == run["message"])

    @staticmethod
    def _insert(conn, rule, planned, now, *, trigger, batch_id=None, status="pending", detail="", is_daily=True):
        contact = conn.execute("SELECT * FROM contacts WHERE id=?", (rule["contact_id"],)).fetchone()
        stamp = now.isoformat()
        run_id = str(uuid4())
        conn.execute("""INSERT INTO runs(id,rule_id,recipient,message,scheduled_at,scheduled_date,
            started_at,finished_at,status,detail,contact_id,platform_id,profile_url,account_id,account_epoch,
            trigger,batch_id,eligible_at,submission,code,created_at,queue_order,conversation_id,conversation_type,is_daily)
            VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,'not_submitted','',?,?,?,?,?)
            ON CONFLICT(rule_id,scheduled_date) WHERE is_daily=1 DO NOTHING""", (
            run_id, rule["id"], contact["display_name"] if contact else rule["recipient"], rule["message"],
            planned.isoformat(), planned.date().isoformat(), stamp if status != "pending" else None, status, detail,
            rule["contact_id"], contact["platform_id"] if contact else None, contact["profile_url"] if contact else None,
            rule["account_id"], rule["account_epoch"], trigger, batch_id,
            stamp if trigger == "manual" else planned.isoformat(), stamp, Scheduler._next_order(conn),
            contact["conversation_id"] if contact else None, contact["conversation_type"] if contact else "unknown", int(is_daily)))
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None and is_daily:
            row = conn.execute("SELECT * FROM runs WHERE rule_id=? AND scheduled_date=? AND is_daily=1",
                               (rule["id"], planned.date().isoformat())).fetchone()
        return dict(row)

    @staticmethod
    def _expire(conn, now):
        conn.execute("UPDATE runs SET status='missed',finished_at=?,code='day_expired',"
                     "detail='已跨过北京时间当天，不再发送' WHERE status='pending' AND scheduled_date<?",
                     (now.isoformat(), now.date().isoformat()))
        conn.execute("UPDATE batches SET status='expired',detail='已跨天，不再恢复昨日批次' "
                     "WHERE status IN ('queued','resume_required') AND scheduled_date<?", (now.date().isoformat(),))
        conn.execute("UPDATE batches SET status='completed' WHERE status='queued' AND NOT EXISTS "
                     "(SELECT 1 FROM runs WHERE runs.batch_id=batches.id AND runs.status IN ('pending','running'))")

    def _materialize(self, conn, now):
        for row in conn.execute("SELECT * FROM rules WHERE enabled=1 AND next_scheduled_at<>''").fetchall():
            rule = self.store._rule_state(conn, dict(row))
            if not rule["sendable"]:
                continue
            candidate = datetime.fromisoformat(rule["next_scheduled_at"])
            # Bounded catch-up: one missed marker summarizes any long shutdown.
            if candidate.date() < now.date():
                self._insert(conn, rule, candidate, now, trigger="scheduled", status="missed",
                             detail="已跨天，停机期间未执行的定时任务已跳过")
                candidate = candidate.replace(year=now.year, month=now.month, day=now.day)
            if candidate <= now:
                self._insert(conn, rule, candidate, now, trigger="scheduled")
                candidate += timedelta(days=1)
            conn.execute("UPDATE rules SET next_scheduled_at=? WHERE id=?", (candidate.isoformat(), rule["id"]))

    def tick(self, now=None, *, exclude_rule_ids=()):
        now = as_local(now if now is not None else local_now())
        stamp = now.isoformat()
        with self.store.transaction() as conn:
            self._materialize(conn, now)
            self._expire(conn, now)
            account = self.store._account(conn)
            if (self._setting(conn, "paused", False) or self._setting(conn, "login_review_required", False)
                    or not self._setting(conn, "account_ready", False) or not account["verified"]
                    or conn.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone()):
                return None
            held = set(exclude_rule_ids)
            rows = conn.execute("""SELECT runs.* FROM runs JOIN rules ON rules.id=runs.rule_id
                LEFT JOIN batches ON batches.id=runs.batch_id
                WHERE runs.status='pending' AND runs.eligible_at<=? AND runs.scheduled_date=?
                AND (runs.trigger='manual' OR rules.enabled=1)
                AND (runs.batch_id IS NULL OR batches.status='queued')
                ORDER BY CASE WHEN runs.trigger='scheduled' THEN 0 ELSE 1 END,
                    runs.eligible_at,runs.queue_order,runs.id""", (stamp, now.date().isoformat())).fetchall()
            for row in rows:
                run = dict(row)
                if run["rule_id"] in held:
                    continue
                rule = self.store._rule_state(conn, dict(conn.execute("SELECT * FROM rules WHERE id=?", (run["rule_id"],)).fetchone()))
                if not rule["sendable"] or not self._snapshot_matches(rule, run):
                    continue
                conn.execute("UPDATE runs SET status='running',started_at=?,finished_at=NULL,detail='',code='' WHERE id=? AND status='pending'", (stamp, run["id"]))
                return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone())
            return None

    def enqueue_manual(self, rule_ids, now=None, *, action_id=None, acknowledge_uncertain_ids=()):
        """Each explicit click may create a new message; replaying that click never does.

        Acknowledgements identify the exact latest unknown run; they authorize a
        NEW request and never reset the old ledger request. Failed, proven
        unsubmitted requests instead retain their ID and immutable payload.
        """
        now = as_local(now if now is not None else local_now())
        stamp, date = now.isoformat(), now.date().isoformat()
        if isinstance(rule_ids, str):
            raise ValueError("发送项应为任务 ID 列表")
        rule_ids = list(dict.fromkeys(rule_ids))
        if any(not isinstance(rule_id, str) or not rule_id for rule_id in rule_ids):
            raise ValueError("任务 ID 无效")
        acknowledgements = sorted(set(acknowledge_uncertain_ids))
        if any(not isinstance(run_id, str) or not run_id for run_id in acknowledgements):
            raise ValueError("待核对记录 ID 无效")
        batch_id = action_id if action_id is not None else str(uuid4())
        if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", batch_id):
            raise ValueError("操作 ID 无效")
        result = {"batch_id": batch_id, "accepted": [], "rejected": [], "replayed": False}
        with self.store.transaction() as conn:
            self._expire(conn, now)
            account = self.store._account(conn)
            signature = json.dumps({"rule_ids": rule_ids, "acknowledge_uncertain_ids": acknowledgements,
                                    "date": date, "account_id": account["platform_id"],
                                    "account_epoch": account["account_epoch"]}, sort_keys=True, ensure_ascii=False)
            previous_action = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if previous_action is not None:
                if previous_action["action_signature"] != signature:
                    raise ValueError("该操作 ID 已用于不同请求，请使用新的操作 ID")
                recorded = json.loads(previous_action["action_result"])
                result["accepted"] = [dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
                                      for run_id in recorded["accepted_ids"]]
                result["rejected"] = recorded["rejected"]
                result["replayed"] = True
                return result
            conn.execute("INSERT INTO batches(id,created_at,scheduled_date,account_id,account_epoch,status,action_signature) "
                         "VALUES(?,?,?,?,?,'queued',?)", (batch_id, stamp, date, account["platform_id"] or "", account["account_epoch"], signature))
            for rule_id in rule_ids:
                row = conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
                code, detail = "", ""
                rule = self.store._rule_state(conn, dict(row)) if row else None
                active = conn.execute("SELECT * FROM runs WHERE rule_id=? AND status IN ('pending','running') "
                                      "ORDER BY queue_order DESC LIMIT 1", (rule_id,)).fetchone()
                latest = conn.execute("SELECT * FROM runs WHERE rule_id=? AND scheduled_date=? "
                                      "ORDER BY queue_order DESC,created_at DESC,id DESC LIMIT 1", (rule_id, date)).fetchone()
                daily = conn.execute("SELECT * FROM runs WHERE rule_id=? AND scheduled_date=? AND is_daily=1", (rule_id, date)).fetchone()
                reuse = None
                if rule is None:
                    code, detail = "rule_missing", "任务已不存在"
                elif not rule["sendable"]:
                    code, detail = "friend_unverified", rule["blocked_reason"]
                elif active is not None:
                    if active["status"] == "pending" and active["trigger"] == "scheduled" and active["scheduled_date"] == date:
                        if self._snapshot_matches(rule, dict(active)):
                            reuse = active
                        else:
                            code, detail = "snapshot_changed", "原定时请求与当前配置不符，不能修改请求后重试"
                    else:
                        code, detail = "already_queued", "这条发送项正在排队或执行；重启后请使用继续批次"
                elif latest is not None:
                    old = dict(latest)
                    if old["status"] == "failed" and old["submission"] == "not_submitted":
                        if self._identity_matches(rule, old):
                            reuse = latest
                        else:
                            code, detail = "snapshot_changed", "原失败请求的账号、好友或会话已变化，不能重试原请求"
                    elif old["status"] == "uncertain" or old["submission"] not in {"not_submitted", "confirmed"}:
                        if old["id"] not in acknowledgements:
                            code, detail = "confirmation_required", "上次结果待核对；确认后可发送一条新消息，原记录不会重试"
                    elif old["status"] == "cancelled" and old["code"] == "schedule_disabled" and self._snapshot_matches(rule, old):
                        reuse = latest
                if code:
                    rejected = {"rule_id": rule_id, "code": code, "detail": detail}
                    if code == "confirmation_required":
                        rejected["run_id"] = latest["id"]
                    result["rejected"].append(rejected)
                    continue
                if reuse is not None:
                    # Never rewrite the request's message or recipient/account identity.
                    conn.execute("UPDATE runs SET trigger='manual',batch_id=?,eligible_at=?,queue_order=?,status='pending',started_at=NULL,"
                                 "finished_at=NULL,detail='',code='' WHERE id=?", (batch_id, stamp, self._next_order(conn), reuse["id"]))
                    run = dict(conn.execute("SELECT * FROM runs WHERE id=?", (reuse["id"],)).fetchone())
                else:
                    planned = now
                    if daily is None and rule["send_time"]:
                        hour, minute = map(int, rule["send_time"].split(":"))
                        planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    run = self._insert(conn, rule, planned, now, trigger="manual", batch_id=batch_id, is_daily=daily is None)
                result["accepted"].append(run)
            recorded = json.dumps({"accepted_ids": [run["id"] for run in result["accepted"]],
                                   "rejected": result["rejected"]}, ensure_ascii=False)
            conn.execute("UPDATE batches SET action_result=?,status=? WHERE id=?", (
                recorded, "queued" if result["accepted"] else "completed", batch_id))
        return result

    def complete(self, run_id, status, detail="", *, submission=None, code=""):
        if not isinstance(status, str) or not status.strip() or status in {"pending", "running"}:
            raise ValueError("完成状态必须是终态")
        if submission is None:
            submission = "confirmed" if status in {"persisted_in_chat", "success"} else "possible" if status == "uncertain" else "unknown"
        if submission not in {"not_submitted", "possible", "confirmed", "unknown"}:
            raise ValueError("提交状态无效")
        if submission == "possible":
            status = "uncertain"
        with self.store.transaction() as conn:
            conn.execute("UPDATE runs SET status=?,finished_at=?,detail=?,submission=?,code=? WHERE id=? AND status='running'", (
                status, local_now().isoformat(), str(detail), submission, str(code), run_id))

    def defer(self, run_id, detail="", *, submission="not_submitted", code=""):
        if submission != "not_submitted":
            return False
        now = local_now()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=? AND status='running'", (run_id,)).fetchone()
            if row is None:
                return False
            run = dict(row)
            current = conn.execute("SELECT * FROM rules WHERE id=?", (run["rule_id"],)).fetchone()
            rule = self.store._rule_state(conn, dict(current)) if current else None
            valid = rule is not None and rule["sendable"] and self._snapshot_matches(rule, run) and (run["trigger"] == "manual" or rule["enabled"])
            status = "pending" if valid else "cancelled"
            if run["scheduled_date"] < now.date().isoformat():
                status = "missed"
            conn.execute("UPDATE runs SET status=?,started_at=NULL,finished_at=?,detail=?,submission='not_submitted',code=? WHERE id=?", (
                status, None if status == "pending" else now.isoformat(), str(detail), str(code), run_id))
            self.store._put_setting(conn, "account_ready", False)
            return True

    def retry_run(self, run_id, now=None):
        now = as_local(now if now is not None else local_now())
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "failed" or row["submission"] != "not_submitted":
                raise ValueError("只能重试已明确未提交的失败任务；待核对结果不能重发")
            run = dict(row)
            if run["scheduled_date"] != now.date().isoformat():
                raise ValueError("只能重试北京时间当天的任务")
            current = conn.execute("SELECT * FROM rules WHERE id=?", (run["rule_id"],)).fetchone()
            rule = self.store._rule_state(conn, dict(current)) if current else None
            if rule is None or not rule["sendable"] or not self._identity_matches(rule, run):
                raise ValueError("账号、好友或任务内容已变化，不能重试原请求")
            if conn.execute("SELECT 1 FROM runs WHERE rule_id=? AND status IN ('pending','running')", (run["rule_id"],)).fetchone():
                raise ValueError("这条发送项正在排队或执行，不能重复加入")
            batch_id = str(uuid4())
            conn.execute("INSERT INTO batches(id,created_at,scheduled_date,account_id,account_epoch,status) "
                         "VALUES(?,?,?,?,?,'queued')", (batch_id, now.isoformat(), run["scheduled_date"], run["account_id"], run["account_epoch"]))
            conn.execute("UPDATE runs SET status='pending',trigger='manual',batch_id=?,eligible_at=?,queue_order=?,started_at=NULL,"
                         "finished_at=NULL,detail='',code='' WHERE id=?", (batch_id, now.isoformat(), self._next_order(conn), run_id))
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def recover_interrupted(self):
        """Startup only, after ensuring no previous worker is active."""
        with self.store.transaction() as conn:
            result = conn.execute("UPDATE runs SET status='uncertain',submission='possible',finished_at=?,code='interrupted',"
                                  "detail='上次执行被中断，结果待核对，不自动重试' WHERE status='running'", (local_now().isoformat(),))
            conn.execute("UPDATE batches SET status='resume_required',detail='程序已重启，请确认后继续剩余手动队列' "
                         "WHERE status='queued' AND EXISTS(SELECT 1 FROM runs WHERE runs.batch_id=batches.id AND runs.status='pending')")
            return result.rowcount

    def list_resumable_batches(self, now=None):
        now = as_local(now if now is not None else local_now())
        with self.store.transaction() as conn:
            self._expire(conn, now)
            account = self.store._account(conn)
            return [dict(row) for row in conn.execute("""SELECT batches.*,COUNT(runs.id) AS pending_count
                FROM batches JOIN runs ON runs.batch_id=batches.id AND runs.status='pending'
                WHERE batches.status='resume_required' AND batches.account_id=? AND batches.account_epoch=?
                AND batches.scheduled_date=? GROUP BY batches.id ORDER BY batches.created_at,batches.id""",
                (account["platform_id"], account["account_epoch"], now.date().isoformat()))]

    def resume_batch(self, batch_id, now=None):
        now = as_local(now if now is not None else local_now())
        with self.store.transaction() as conn:
            self._expire(conn, now)
            batch = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            account = self.store._account(conn)
            if (batch is None or batch["status"] != "resume_required" or batch["scheduled_date"] != now.date().isoformat()
                    or batch["account_id"] != account["platform_id"] or batch["account_epoch"] != account["account_epoch"] or not account["verified"]):
                raise ValueError("该批次已跨天、账号已变化或无需继续")
            count = conn.execute("SELECT COUNT(*) FROM runs WHERE batch_id=? AND status='pending'", (batch_id,)).fetchone()[0]
            conn.execute("UPDATE batches SET status='queued',detail='' WHERE id=?", (batch_id,))
            return count
