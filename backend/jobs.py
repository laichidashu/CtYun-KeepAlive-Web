# -*- coding: utf-8 -*-
"""
定时任务服务（对应 C# JobService.cs + CronScheduler.cs + Models/JobModels.cs）。
- jobs.json：ScheduledJob 列表（字段名与 .NET 版逐字一致）
- jobs_history.json：JobRunRecord 列表，上限 50 条，最新在前
- 调度循环每 20s 扫描一次启用任务，到点触发
"""
import random
import threading
import time
import uuid
from datetime import datetime, timedelta

import cronx
import keepalive
import logs
import store
from store import G, ConfigStore, Paths

TICK_SECONDS = 20
HISTORY_LIMIT = 50

JOB_TYPE_AI_CHAT = "ai_chat"
JOB_TYPE_PC_HANG = "pc_hang"


def job_type_valid(t: str) -> bool:
    return t in (JOB_TYPE_AI_CHAT, JOB_TYPE_PC_HANG)


class ScheduledJob:
    def __init__(self):
        self.id = ""
        self.name = ""
        self.type = JOB_TYPE_AI_CHAT
        self.cron = "0 3,20 * * *"
        self.enabled = True
        self.account_user = ""     # 关联账号主键（手机号）
        self.timeout_minutes = 15
        self.hang_seconds = 4800   # 仅 pc_hang 生效
        self.random_daily = False  # 当天随机时间运行（开启后每天自动随机选一个时刻）
        # 运行时字段（持久化，由服务端维护）
        self.last_run_at = ""      # "yyyy-MM-dd HH:mm:ss"
        self.next_run_at = ""      # 空串表示无有效触发时间
        self.last_result = ""
        self.running = False       # 进程内瞬时标记，重启后复位

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "cron": self.cron,
            "enabled": self.enabled,
            "accountUser": self.account_user,
            "timeoutMinutes": self.timeout_minutes,
            "hangSeconds": self.hang_seconds,
            "randomDaily": self.random_daily,
            "lastRunAt": self.last_run_at,
            "nextRunAt": self.next_run_at,
            "lastResult": self.last_result,
            "running": self.running,
        }

    @classmethod
    def from_dict(cls, d):
        j = cls()
        j.id = d.get("id", "") or ""
        j.name = d.get("name", "") or ""
        j.type = d.get("type", JOB_TYPE_AI_CHAT) or JOB_TYPE_AI_CHAT
        j.cron = d.get("cron", "0 3,20 * * *") or "0 3,20 * * *"
        j.enabled = bool(d.get("enabled", True))
        j.account_user = d.get("accountUser", "") or ""
        j.timeout_minutes = int(d.get("timeoutMinutes", 15))
        j.hang_seconds = int(d.get("hangSeconds", 4800))
        j.random_daily = bool(d.get("randomDaily", False))
        j.last_run_at = d.get("lastRunAt", "") or ""
        j.next_run_at = d.get("nextRunAt", "") or ""
        j.last_result = d.get("lastResult", "") or ""
        j.running = False
        return j


class JobRunRecord:
    def __init__(self):
        self.id = ""
        self.job_id = ""
        self.job_name = ""
        self.job_type = ""
        self.account_user = ""
        self.started_at = 0     # Unix 秒
        self.ended_at = 0
        self.duration_seconds = 0
        self.exit_code = 0
        self.success = False
        self.timed_out = False
        self.summary = ""       # 最多 500 字

    def to_dict(self):
        return {
            "id": self.id,
            "jobId": self.job_id,
            "jobName": self.job_name,
            "jobType": self.job_type,
            "accountUser": self.account_user,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "durationSeconds": self.duration_seconds,
            "exitCode": self.exit_code,
            "success": self.success,
            "timedOut": self.timed_out,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d):
        r = cls()
        r.id = d.get("id", "") or ""
        r.job_id = d.get("jobId", "") or ""
        r.job_name = d.get("jobName", "") or ""
        r.job_type = d.get("jobType", "") or ""
        r.account_user = d.get("accountUser", "") or ""
        r.started_at = int(d.get("startedAt", 0))
        r.ended_at = int(d.get("endedAt", 0))
        r.duration_seconds = int(d.get("durationSeconds", 0))
        r.exit_code = int(d.get("exitCode", 0))
        r.success = bool(d.get("success", False))
        r.timed_out = bool(d.get("timedOut", False))
        r.summary = d.get("summary", "") or ""
        return r


class JobService:
    _jobs = []            # List[ScheduledJob]
    _lock = threading.RLock()
    is_running = False    # 调度器整体运行状态（由 CronScheduler 维护）

    @classmethod
    def initialize(cls):
        """加载 jobs.json，复位运行态并重算下次触发时间。"""
        with cls._lock:
            cls._jobs = ConfigStore.load(
                Paths.jobs_path,
                lambda raw: [ScheduledJob.from_dict(x) for x in raw],
                lambda: [])
            for j in cls._jobs:
                j.running = False
            for j in cls._jobs:
                cls.recalc_next_run(j)

    @classmethod
    def snapshot(cls):
        with cls._lock:
            return [j.to_dict() for j in cls._jobs]

    @classmethod
    def history_snapshot(cls):
        records = ConfigStore.load(
            Paths.jobs_history_path,
            lambda raw: [JobRunRecord.from_dict(x) for x in raw],
            lambda: [])
        return [r.to_dict() for r in records]

    @classmethod
    def recalc_next_run(cls, job: ScheduledJob):
        try:
            if job.random_daily:
                nxt = cls._random_next_daily(job)
                job.next_run_at = nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else ""
                return
            cron = cronx.CronExpression.try_parse(job.cron)
            nxt = cron.get_next_occurrence(datetime.now(), datetime.now() + timedelta(days=366))
            job.next_run_at = nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else ""
        except cronx.CronError:
            job.next_run_at = ""

    @classmethod
    def _random_next_daily(cls, job: ScheduledJob):
        """当天随机时间调度：每天在 00:00–23:59 内均匀随机选一个时刻运行一次。

        - 今天已跑过（last_run_at 为今天）→ 排明天；
        - 今天的随机点若已过当前时刻，则在「剩余时间窗」内随机，保证今天仍会跑；
        - 随机结果至少晚于当前 2 分钟，防止刚安排就触发。
        """
        now = datetime.now()
        day = now.date()
        last = None
        if job.last_run_at:
            try:
                last = datetime.strptime(job.last_run_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                last = None
        if last is not None and last.date() >= day:
            day = last.date() + timedelta(days=1)
        for _ in range(10):
            day_start = datetime.combine(day, datetime.min.time())
            lo = 0
            if day == now.date():
                # 今天：在剩余时间窗内随机（至少晚 2 分钟）
                lo = min(86399, int((now - day_start).total_seconds()) + 120)
            if lo >= 86399:
                day = day + timedelta(days=1)
                continue
            return day_start + timedelta(seconds=random.randint(lo, 86359))
        return None

    @classmethod
    def find(cls, job_id: str):
        """按 Id 查找内部对象（真实引用，修改 Running 即生效）。"""
        if not job_id:
            return None
        with cls._lock:
            for j in cls._jobs:
                if j.id == job_id:
                    return j
        return None

    @classmethod
    def upsert(cls, job: ScheduledJob):
        with cls._lock:
            existing = cls._find_locked(job.id)
            if existing is not None:
                cls._jobs.remove(existing)
            cls._jobs.append(job)
            cls.recalc_next_run(job)
            cls._persist_locked()

    @classmethod
    def remove(cls, job_id: str):
        with cls._lock:
            existing = cls._find_locked(job_id)
            if existing is not None:
                cls._jobs.remove(existing)
                cls._persist_locked()

    @classmethod
    def execute(cls, job: ScheduledJob, source: str):
        """核心执行体：账号解析 → 浏览器互斥 → 脚本运行 → 历史记录。任何异常兜底封装。"""
        import scriptrunner
        record = None
        acquired = False
        try:
            with cls._lock:
                job.running = True
            logs.info("任务", "[%s] 开始执行任务：%s（%s）" % (source, job.name, job.type))

            record = JobRunRecord()
            record.id = uuid.uuid4().hex
            record.job_id = job.id
            record.job_name = job.name
            record.job_type = job.type
            record.account_user = job.account_user
            record.started_at = int(time.time())

            # 1. 关联账号解析
            account_key = keepalive.resolve_key(job.account_user, G.config.accounts)
            if account_key is None:
                record.success = False
                record.summary = "关联账号不存在"
                record.ended_at = int(time.time())
                job.last_result = "关联账号不存在"
                with cls._lock:
                    job.running = False
                cls._append_history(record)
                return
            account = keepalive.find_account(account_key, G.config.accounts)

            # 1.5 平台任务预检：读取账号的平台任务列表，已完成则跳过执行。
            #     fail-open：登录失败 / 无法判断时照常执行脚本，绝不因预检误跳过。
            keyword = "使用1小时" if job.type == JOB_TYPE_PC_HANG else "对话"
            try:
                import ctyun_api
                import redeem
                api = ctyun_api.CtYunApi(account.device_code)
                if not api.login(account.user, account.password):
                    logs.warn("任务", "[%s] 平台任务预检登录失败，照常执行脚本" % job.name)
                else:
                    done, desc = redeem.probe_task_done(api, keyword)
                    logs.info("任务", "[%s] 平台任务预检（关键词「%s」）：%s" % (job.name, keyword, desc))
                    if done is True:
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        record.success = True
                        record.ended_at = int(time.time())
                        record.duration_seconds = max(0, record.ended_at - record.started_at)
                        record.summary = "平台任务已完成，无需执行（%s）｜%s" % (keyword, desc)
                        job.last_run_at = now_str
                        job.last_result = "已完成，跳过"
                        with cls._lock:
                            job.running = False
                        cls._append_history(record)
                        logs.info("任务", "[%s] 平台任务已完成，跳过执行" % job.name)
                        return
            except Exception as pex:
                logs.warn("任务", "[%s] 平台任务预检异常（照常执行脚本）：%s" % (job.name, pex))

            # 2. 浏览器互斥
            import mutex
            if not mutex.BrowserMutex.try_acquire(job.type, job.name):
                record.success = False
                record.summary = mutex.mutex_message()
                record.ended_at = int(time.time())
                job.last_result = "被互斥跳过"
                with cls._lock:
                    job.running = False
                cls._append_history(record)
                return
            acquired = True

            # 3. 脚本与参数
            script_path = Paths.pc_hang_script if job.type == JOB_TYPE_PC_HANG else Paths.ai_chat_script
            hang_seconds = job.hang_seconds if job.type == JOB_TYPE_PC_HANG else 0

            # 4. 运行脚本
            result = scriptrunner.run(account, script_path, hang_seconds, job.timeout_minutes)

            # 5. 汇总
            ended = int(time.time())
            record.ended_at = ended
            record.duration_seconds = max(0, ended - record.started_at)
            record.exit_code = result["exitCode"]
            record.timed_out = result["timedOut"]
            record.success = (result["exitCode"] == 0 and not result["timedOut"] and not result["startFailed"])
            record.summary = result["summary"]

            job.last_run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if record.success:
                job.last_result = "成功"
            elif result["timedOut"]:
                job.last_result = "超时"
            elif result["startFailed"]:
                job.last_result = "启动失败"
            else:
                job.last_result = "失败"

            with cls._lock:
                job.running = False
            cls._append_history(record)
            logs.info("任务", "[%s] 任务 %s 结束：%s（退出码 %d）"
                      % (source, job.name, job.last_result, record.exit_code))
        except Exception as ex:
            logs.fail("任务", "执行异常：" + str(ex))
            if record is not None:
                record.success = False
                record.ended_at = int(time.time())
                record.summary = "执行异常：" + str(ex)
                cls._append_history(record)
            try:
                with cls._lock:
                    job.running = False
            except Exception:
                pass
        finally:
            if acquired:
                import mutex
                mutex.BrowserMutex.release(job.type)
            # 随机调度：执行结束后重算下一次（last_run_at 已更新 → 排到明天随机时刻）
            if job.random_daily:
                try:
                    cls.recalc_next_run(job)
                except Exception:
                    pass

    @classmethod
    def today_stats(cls):
        """今日（按 startedAt 日期）成功/失败数。"""
        today = datetime.now().date()
        s = f = 0
        for r in cls.history_snapshot():
            dt = datetime.fromtimestamp(r["startedAt"]).date()
            if dt == today:
                if r["success"]:
                    s += 1
                else:
                    f += 1
        return s, f

    @classmethod
    def next_job(cls):
        """启用且 NextRunAt 有效的任务中，最小的未来触发时间与名称。"""
        best, name = 0, ""
        now = datetime.now()
        with cls._lock:
            for j in cls._jobs:
                if not j.enabled or not j.next_run_at:
                    continue
                try:
                    nxt = datetime.strptime(j.next_run_at, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if nxt <= now:
                    continue
                unix = int(nxt.timestamp())
                if best == 0 or unix < best:
                    best, name = unix, j.name
        return best, name

    # ---------- 内部 ----------

    @classmethod
    def _find_locked(cls, job_id):
        for j in cls._jobs:
            if j.id == job_id:
                return j
        return None

    @classmethod
    def _persist_locked(cls):
        ConfigStore.save(Paths.jobs_path, lambda: [j.to_dict() for j in cls._jobs])

    @classmethod
    def _append_history(cls, record: JobRunRecord):
        records = ConfigStore.load(
            Paths.jobs_history_path,
            lambda raw: [JobRunRecord.from_dict(x) for x in raw],
            lambda: [])
        records.insert(0, record)
        while len(records) > HISTORY_LIMIT:
            records.pop()
        ConfigStore.save(Paths.jobs_history_path, lambda: [r.to_dict() for r in records])


class CronScheduler:
    """调度主循环：每 20s 扫描启用任务，到点触发。不自行启动，由 server.main 启动。"""

    @classmethod
    def run_loop(cls, stop_event: threading.Event):
        JobService.is_running = True
        try:
            while not stop_event.is_set() and not G.global_stop.is_set():
                try:
                    now = datetime.now()
                    for job_dict in JobService.snapshot():
                        if not job_dict["enabled"] or job_dict["running"]:
                            continue
                        real = JobService.find(job_dict["id"])
                        if real is None:
                            continue
                        if not real.next_run_at:
                            JobService.recalc_next_run(real)
                            continue
                        try:
                            nxt = datetime.strptime(real.next_run_at, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            JobService.recalc_next_run(real)
                            continue
                        if now >= nxt:
                            # 随机调度：由 execute 结束后按"今天已跑过"重算下次，避免触发前重复随机
                            if not real.random_daily:
                                JobService.recalc_next_run(real)
                            threading.Thread(
                                target=JobService.execute, args=(real, "cron"),
                                name="job-" + real.id, daemon=True).start()
                except Exception as ex:
                    logs.fail("调度", "调度循环异常：" + str(ex))

                if stop_event.wait(TICK_SECONDS) or G.global_stop.wait(0):
                    break
        finally:
            JobService.is_running = False
