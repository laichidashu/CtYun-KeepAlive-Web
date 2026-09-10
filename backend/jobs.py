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
# 定时触发的任务在浏览器被占用时的排队参数（避免多个任务同时到点互相顶掉）
MUTEX_QUEUE_WAIT_SECONDS = 3 * 3600   # 最多排队 3 小时
RANDOM_JITTER_MINUTES = 90            # random_daily：cron 时刻后的随机抖动窗口（分钟）。
                                      # 需小于错峰间隔（挂机任务间隔 100 分钟），否则窗口重叠仍可能撞车。
MUTEX_QUEUE_POLL_SECONDS = 20         # 每 20 秒重试一次
ORPHAN_GRACE_SECONDS = 300            # 「锁被持有但无运行中任务」持续多久才判定为泄漏并回收
                                      # （规避「任务刚结束、finally 尚未释放」的毫秒级窗口）

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
        self.queued = False        # 进程内瞬时标记：正在排队等待浏览器互斥锁
        self.run_started_at = ""   # 进程内瞬时标记：本次运行开始时间（前端横幅计时用）

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
            "queued": self.queued,
            "runStartedAt": self.run_started_at,
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
        j.queued = False
        j.run_started_at = ""
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
    _orphan_since = {}    # 互斥锁键 → 首次发现「被持有但无运行中任务」的时间戳（识别泄漏锁）

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
        """随机调度（random_daily）：在每个 cron 触发点之后 0~RANDOM_JITTER_MINUTES
        分钟的窗口内随机运行一次。

        旧实现是全日 00:00–23:59 均匀随机——多个任务各自全日随机，
        80 分钟的挂机任务极易两两撞车 → 排队 → 3 小时上限 → 频繁「超时」，
        这正是「定时任务从早跑到晚都跑不完」的根源。
        新实现以 cron 排布为基准 + 窗口内抖动：既保留防风控的随机性，
        又让错峰排布真正可控。抖动值按（任务 Id + 触发点）确定性生成，
        避免重算 next_run_at 时来回跳动。
        """
        cron = cronx.CronExpression.try_parse(job.cron)
        now = datetime.now()
        base = cron.get_next_occurrence(now, now + timedelta(days=366))
        if not base:
            return None
        rng = random.Random("%s|%s" % (job.id, base.strftime("%Y-%m-%dT%H:%M")))
        delay = rng.uniform(0, RANDOM_JITTER_MINUTES * 60)
        nxt = base + timedelta(seconds=delay)
        if nxt <= now + timedelta(seconds=110):
            # 抖动后落在眼前：至少推后 2 分钟，防止刚排定立即触发
            nxt = now + timedelta(minutes=2)
        return nxt

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
                # 本次运行开始时间：前端「当前浏览器任务」横幅据此计算已运行时长。
                # 不写 last_run_at（其在运行结束时落盘，被随机调度用于"排明天"判定）。
                job.run_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

            # 2. 浏览器互斥（定时触发的任务排队等待，避免同时到点全部被跳过）
            import mutex
            # 关键：无论「首次直接获取成功」还是「排队后获取成功」，都必须让
            # acquired=True，否则 finally 不会调用 release → 互斥锁永久泄漏，
            # 之后所有同类任务排队 3 小时也拿不到锁（历史事故根因：
            # 15:09 一个 ai_chat 任务直接拿到锁却未释放，导致当天全部
            # pc_hang 任务 18:48 起持续「被互斥跳过」，挂机任务全废）。
            # run_token：本次运行的锁持有者令牌，finally 只释放自己持有的锁，
            # 避免「被停止强制释放后锁已被他人拿走，自己的 finally 又误放他人锁」。
            run_token = uuid.uuid4().hex
            acquired = mutex.BrowserMutex.try_acquire(job.type, job.name, run_token)
            if not acquired:
                with cls._lock:
                    job.queued = True  # 前端「排队等待」横幅标记
                if source == "cron":
                    logs.info("任务", "[%s] 浏览器被占用，进入排队等待（最多 %d 分钟）"
                              % (job.name, MUTEX_QUEUE_WAIT_SECONDS // 60))
                    waited = 0
                    while waited < MUTEX_QUEUE_WAIT_SECONDS:
                        time.sleep(MUTEX_QUEUE_POLL_SECONDS)
                        waited += MUTEX_QUEUE_POLL_SECONDS
                        with cls._lock:
                            # stop 会把 running 置 False：排队线程据此退出，不再抢锁执行
                            still_enabled = job.enabled and job.running
                        if not still_enabled:
                            break
                        if mutex.BrowserMutex.try_acquire(job.type, job.name, run_token):
                            acquired = True
                            logs.info("任务", "[%s] 排队 %d 秒后获得浏览器使用权，开始执行"
                                      % (job.name, waited))
                            break
                if not acquired:
                    record.success = False
                    record.summary = mutex.mutex_message()
                    record.ended_at = int(time.time())
                    job.last_result = "被互斥跳过"
                    with cls._lock:
                        job.running = False
                    cls._append_history(record)
                    return

            # 3. 脚本与参数
            script_path = Paths.pc_hang_script if job.type == JOB_TYPE_PC_HANG else Paths.ai_chat_script
            hang_seconds = job.hang_seconds if job.type == JOB_TYPE_PC_HANG else 0

            # 3.5 挂机任务的生效超时：必须覆盖挂机时长，否则任务必然被判超时
            #     （例如挂机 4810 秒却只给 15 分钟超时，脚本再正常也会被杀）。
            effective_timeout = job.timeout_minutes
            if job.type == JOB_TYPE_PC_HANG:
                needed = hang_seconds // 60 + 10  # 挂机时长 + 10 分钟收尾余量
                if effective_timeout < needed:
                    effective_timeout = needed
                    logs.warn("任务", "[%s] 超时时间 %d 分钟小于挂机时长（%d 秒），"
                              "本次按 %d 分钟执行；建议在任务里把超时调到该值以上"
                              % (job.name, job.timeout_minutes, hang_seconds, needed))

            # 4. 运行脚本（job_type 用于进程登记键「账号:类型」，防止同账号
            #    并行任务互相覆盖登记表导致进程无法停止/泄漏）
            result = scriptrunner.run(account, script_path, hang_seconds,
                                      effective_timeout, job_type=job.type)

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
                # 只释放本次运行持有的锁（令牌匹配）；若已被 stop 强制释放且
                # 锁被别的任务拿走，这里会被拒绝，不会误放他人锁。
                mutex.BrowserMutex.release(job.type, token=run_token)
            # 复位前端横幅所需的瞬时标记（execute 任何路径退出都会经过这里）
            try:
                with cls._lock:
                    job.queued = False
                    job.run_started_at = ""
            except Exception:
                pass
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
    def task_summary(cls):
        """按账号汇总平台任务（ai_chat / pc_hang）的执行情况。

        返回 {date, accounts: [{accountUser, tasks: [...], aiChatDone, aiChatMissing}]}。
        判定口径：某任务「今日已完成」= 今天有 success=true 的执行记录
        （含平台预检判定「已完成，跳过」的记录）。
        """
        with cls._lock:
            jobs = list(cls._jobs)
        hist = cls.history_snapshot()
        today = datetime.now().date()
        accounts = {}
        order = []
        for j in jobs:
            key = j.account_user or "(未绑定账号)"
            if key not in accounts:
                accounts[key] = {"accountUser": key,
                                 "tasks": [], "aiChatDone": True, "aiChatMissing": []}
                order.append(key)
            entry = {
                "jobId": j.id, "jobName": j.name, "jobType": j.type,
                "enabled": j.enabled, "running": j.running,
                "lastRunAt": j.last_run_at, "nextRunAt": j.next_run_at,
                "lastResult": j.last_result or "",
            }
            todays = [r for r in hist
                      if (r.get("jobId") == j.id
                          or (r.get("accountUser", "") == (j.account_user or "")
                              and r.get("jobType", "") == j.type))
                      and datetime.fromtimestamp(r.get("startedAt", 0)).date() == today]
            entry["todayRuns"] = len(todays)
            entry["todaySuccess"] = any(r.get("success") for r in todays)
            if todays:
                entry["todayLastSummary"] = todays[-1].get("summary", "")
                entry["todayLastAt"] = datetime.fromtimestamp(
                    todays[-1]["startedAt"]).strftime("%H:%M:%S")
            else:
                entry["todayLastSummary"] = ""
                entry["todayLastAt"] = ""
            accounts[key]["tasks"].append(entry)
            if j.type == JOB_TYPE_AI_CHAT and j.enabled and not entry["todaySuccess"]:
                accounts[key]["aiChatDone"] = False
                accounts[key]["aiChatMissing"].append(j.id)
        return {"date": today.isoformat(), "accounts": [accounts[k] for k in order]}

    @classmethod
    def run_missing_ai_chat(cls):
        """对今日尚未完成 AI 对话任务的账号，**串行**后台触发执行。

        为什么串行：execute() 受浏览器互斥约束（Global 模式全局同一时刻
        只允许一个浏览器）。若并发触发全部缺失任务，除第一个外都会被
        互斥立刻拒绝（"被互斥跳过"），补做等于没做。因此这里只启动
        一个工作线程，逐个执行；每个任务执行前重新判定是否仍缺失
        （前一个任务耗时较长，期间 cron 可能已完成后续任务）。
        execute() 内置平台预检：若平台侧本就已完成（关键词「对话」），
        会直接记录「已完成，跳过」，不会重复跑浏览器。
        """
        summary = cls.task_summary()
        triggered, skipped = [], []
        pending = []
        for acc in summary["accounts"]:
            for jid in acc.get("aiChatMissing", []):
                job = cls.find(jid)
                if job is None:
                    continue
                if job.running:
                    skipped.append({"job": job.name, "reason": "正在运行中"})
                    continue
                pending.append(job)
                triggered.append(job.name)
        if pending:
            threading.Thread(
                target=cls._run_missing_worker, args=(pending,),
                name="job-missing-queue", daemon=True).start()
            logs.info("任务", "[补做] 检测到 %d 个今日未完成的 AI 对话任务，"
                      "将按队列串行执行：%s"
                      % (len(pending), "、".join(triggered)))
        return {"triggered": triggered, "skipped": skipped}

    @classmethod
    def _run_missing_worker(cls, pending):
        """串行消费补做队列：逐个执行，执行前二次确认仍缺失。"""
        for job in pending:
            latest = cls.find(job.id)
            if latest is None:
                continue
            if latest.running:
                continue
            # 二次判定：该任务今日是否已有成功记录（可能被 cron/手动抢先完成）
            try:
                today = datetime.now().date()
                done = any(
                    r.get("jobId") == job.id and r.get("success")
                    and datetime.fromtimestamp(r.get("startedAt", 0)).date() == today
                    for r in cls.history_snapshot())
            except Exception:
                done = False
            if done:
                logs.info("任务", "[补做] %s 已完成（无需重复执行），跳过" % job.name)
                continue
            try:
                cls.execute(latest, "manual")
            except Exception as ex:
                logs.error("任务", "[补做] %s 执行异常：%s" % (job.name, ex))

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

    # 单个任务从触发到结束的理论最长秒数：排队上限 + 生效超时 + 收尾余量。
    @classmethod
    def _max_run_seconds(cls, job: ScheduledJob) -> int:
        eff = job.timeout_minutes
        if job.type == JOB_TYPE_PC_HANG:
            needed = job.hang_seconds // 60 + 10
            if eff < needed:
                eff = needed
        return MUTEX_QUEUE_WAIT_SECONDS + eff * 60 + 600

    @classmethod
    def reap_stale_state(cls):
        """自愈：清理幽灵运行态，并回收泄漏的浏览器互斥锁。

        本方法由调度循环每 tick 调用，覆盖两类「标记/锁未复位」故障：
        1) 幽灵任务：execute 线程被强杀（进程被 kill / 异常吞掉 finally）导致
           job.running 卡在 True，调度器据此永远跳过该任务。
        2) 泄漏锁：任务获取互斥锁后走了未释放的路径，后续同类任务排队 3 小时
           也拿不到锁，永远「被互斥跳过」。
        泄漏判定用不变量「锁被持有 ⇒ 必有 running 任务」；为规避「任务刚结束、
        finally 尚未释放」的毫秒级窗口，要求孤儿状态持续 ORPHAN_GRACE_SECONDS 才回收。
        """
        changed = False
        now = datetime.now()
        with cls._lock:
            # --- 1. 幽灵运行态复位 ---
            for job in cls._jobs:
                if not job.running or not job.run_started_at:
                    continue
                try:
                    started = datetime.strptime(job.run_started_at, "%Y-%m-%d %H:%M:%S")
                    age = (now - started).total_seconds()
                except ValueError:
                    age = 0
                if age > cls._max_run_seconds(job):
                    logs.warn("任务", "[%s] 运行标记已持续 %d 分钟（超过理论上限），"
                              "判定为幽灵任务并复位运行状态" % (job.name, int(age // 60)))
                    job.running = False
                    job.queued = False
                    job.run_started_at = ""
                    changed = True

            # --- 2. 孤儿互斥锁回收 ---
            import mutex
            mode = store.G.config.browser_mutex_mode if store.G.config else "Global"
            if mode == "PerType":
                keys = (JOB_TYPE_AI_CHAT, JOB_TYPE_PC_HANG)

                def _holder_busy(key):
                    return any(j.running and (j.type or "") == key for j in cls._jobs)
            else:
                keys = ("",)

                def _holder_busy(key):
                    return any(j.running for j in cls._jobs)

            for key in keys:
                if not mutex.BrowserMutex.is_held(key) or _holder_busy(key):
                    cls._orphan_since.pop(key, None)
                    continue
                since = cls._orphan_since.get(key)
                if since is None:
                    cls._orphan_since[key] = now
                    logs.warn("任务", "浏览器互斥锁（模式 %s，键 %s）被持有但无运行中任务，"
                              "观察 %d 秒后自动回收" % (mode, key or "-", ORPHAN_GRACE_SECONDS))
                elif (now - since).total_seconds() >= ORPHAN_GRACE_SECONDS:
                    logs.warn("任务", "确认浏览器互斥锁（模式 %s，键 %s）已泄漏，强制释放"
                              % (mode, key or "-"))
                    mutex.BrowserMutex.release(key, force=True)
                    cls._orphan_since.pop(key, None)
                    changed = True

            if changed:
                try:
                    cls._persist_locked()
                except Exception:
                    pass

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
                    # 每 tick 先自愈：复位幽灵运行态、回收泄漏的互斥锁，
                    # 避免「任务永远排队 / 被互斥跳过」的静默失效。
                    JobService.reap_stale_state()
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
