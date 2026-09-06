# -*- coding: utf-8 -*-
"""
5 段 Cron 表达式解析器（对应 C# CronExpression.cs，零依赖）。
字段顺序：分 时 日 月 周。取值范围：分 0-59 / 时 0-23 / 日 1-31 / 月 1-12 / 周 0-7（0 与 7 均为周日）。
不支持：秒（6 段）、?、L、W、#、H、英文月份/星期名、@宏。
使用服务器本地时间。
"""
from datetime import datetime, timedelta

_MINUTE_MIN, _MINUTE_MAX = 0, 59
_HOUR_MIN, _HOUR_MAX = 0, 23
_DOM_MIN, _DOM_MAX = 1, 31
_MONTH_MIN, _MONTH_MAX = 1, 12
_DOW_MIN, _DOW_MAX = 0, 7


def _full_mask(lo, hi):
    m = 0
    for i in range(lo, hi + 1):
        m |= 1 << i
    return m


_DOM_FULL = _full_mask(1, 31)
_DOW_FULL = _full_mask(0, 6)

_ERR_L = "不支持 L / -1 表达式（如月末）：请改用 redeem 的 monthlyDays（值为 -1）以表示月末。"


class CronError(Exception):
    pass


def _next_set_bit(mask: int, from_index: int) -> int:
    if from_index < 0:
        from_index = 0
    i = from_index
    while i < 64:
        if mask & (1 << i):
            return i
        i += 1
    return -1


def _parse_item(item, lo_min, hi_max, seg, fname):
    """解析单项，返回置位片段 mask。失败抛 CronError。"""
    if not item:
        raise CronError("无法解析第 %d 段（%s）：空项" % (seg, fname))
    if "L" in item or "l" in item or item == "-1":
        raise CronError(_ERR_L)
    for c in item:
        if not (c.isdigit() or c in "*/-"):
            raise CronError("无法解析第 %d 段（%s）：%s" % (seg, fname, item))

    left, step = item, 1
    slash = item.find("/")
    if slash >= 0:
        left = item[:slash]
        sstr = item[slash + 1:]
        try:
            step = int(sstr)
        except ValueError:
            raise CronError("步进值必须 ≥ 1")
        if step <= 0:
            raise CronError("步进值必须 ≥ 1")

    if left == "*":
        lo, hi = lo_min, hi_max
    elif "-" in left:
        parts = left.split("-")
        if len(parts) != 2:
            raise CronError("无法解析第 %d 段（%s）：%s" % (seg, fname, item))
        try:
            lo, hi = int(parts[0]), int(parts[1])
        except ValueError:
            raise CronError("无法解析第 %d 段（%s）：%s" % (seg, fname, item))
        if lo > hi:
            raise CronError("范围起始值 %d 大于结束值 %d" % (lo, hi))
    else:
        try:
            lo = int(left)
        except ValueError:
            raise CronError("无法解析第 %d 段（%s）：%s" % (seg, fname, item))
        hi = lo

    if lo < lo_min or hi > hi_max:
        raise CronError("%s取值 %d 超出范围 %d-%d" % (fname, lo, lo_min, hi_max))

    mask = 0
    v = lo
    while v <= hi:
        mask |= 1 << v
        v += step
    return mask


def _parse_field(field, lo_min, hi_max, seg, fname):
    mask = 0
    for raw in field.split(","):
        mask |= _parse_item(raw.strip(), lo_min, hi_max, seg, fname)
    return mask


class CronExpression:
    def __init__(self, minute, hour, dom, month, dow, raw):
        self._minute = minute
        self._hour = hour
        self._dom = dom
        self._month = month
        self._dow = dow
        self._raw = raw

    @staticmethod
    def try_parse(expr):
        """成功返回 CronExpression，失败抛 CronError（error 消息面向用户）。"""
        if not expr or not expr.strip():
            raise CronError("Cron 表达式不能为空")
        # 以任意空白切分并去空段
        fields = expr.split()
        if len(fields) != 5:
            raise CronError("Cron 表达式必须为 5 段（分 时 日 月 周），当前 %d 段" % len(fields))
        minute = _parse_field(fields[0], _MINUTE_MIN, _MINUTE_MAX, 1, "分钟")
        hour = _parse_field(fields[1], _HOUR_MIN, _HOUR_MAX, 2, "时")
        dom = _parse_field(fields[2], _DOM_MIN, _DOM_MAX, 3, "日")
        month = _parse_field(fields[3], _MONTH_MIN, _MONTH_MAX, 4, "月")
        dow = _parse_field(fields[4], _DOW_MIN, _DOW_MAX, 5, "周")
        # 周字段：7 与 0 同为周日，归一化到 bit 0
        if dow & (1 << 7):
            dow |= 1
        return CronExpression(minute, hour, dom, month, dow, expr.strip())

    def _day_matches(self, t: datetime) -> bool:
        dom_restricted = (self._dom & _DOM_FULL) != _DOM_FULL
        dow_restricted = (self._dow & _DOW_FULL) != _DOW_FULL
        dom_match = (self._dom & (1 << t.day)) != 0
        # Python weekday(): 周一=0..周日=6 → C# DayOfWeek: 周日=0..周六=6
        dow_c = (t.weekday() + 1) % 7
        dow_match = (self._dow & (1 << dow_c)) != 0
        if dom_restricted and dow_restricted:
            return dom_match or dow_match
        if dom_restricted:
            return dom_match
        if dow_restricted:
            return dow_match
        return True

    def matches(self, t: datetime) -> bool:
        tt = t.replace(second=0, microsecond=0)
        if not (self._minute & (1 << tt.minute)):
            return False
        if not (self._hour & (1 << tt.hour)):
            return False
        if not (self._month & (1 << tt.month)):
            return False
        return self._day_matches(tt)

    def get_next_occurrence(self, from_dt: datetime, limit: datetime):
        """返回严格大于 from_dt 的下一次命中；limit 内无命中返回 None。"""
        cur = from_dt.replace(second=0, microsecond=0)
        if cur <= from_dt:
            cur = cur + timedelta(minutes=1)

        guard = 0
        while guard < 500000:
            guard += 1
            if cur > limit:
                return None
            # 月
            if not (self._month & (1 << cur.month)):
                cur = datetime(cur.year, cur.month, 1) + timedelta(days=32)
                cur = datetime(cur.year, cur.month, 1)
                continue
            # 日（DOM/DOW 组合语义）
            if not self._day_matches(cur):
                cur = datetime(cur.year, cur.month, cur.day) + timedelta(days=1)
                continue
            # 时
            if not (self._hour & (1 << cur.hour)):
                nh = _next_set_bit(self._hour, cur.hour + 1)
                if nh >= 0:
                    cur = cur.replace(hour=nh, minute=0)
                else:
                    cur = datetime(cur.year, cur.month, cur.day) + timedelta(days=1)
                continue
            # 分
            if not (self._minute & (1 << cur.minute)):
                nm = _next_set_bit(self._minute, cur.minute + 1)
                if nm >= 0:
                    cur = cur.replace(minute=nm)
                else:
                    cur = cur.replace(minute=0) + timedelta(hours=1)
                continue
            return cur
        return None

    def describe(self) -> str:
        table = {
            "0 3,20 * * *": "每天 03:00 和 20:00",
            "*/10 * * * *": "每 10 分钟",
            "0 * * * *": "每小时整点",
            "0 4 * * 1-5": "周一至周五 04:00",
            "0 4 * * 0,6": "周日、周六 04:00",
            "30 9 * * *": "每天 09:30",
            "0 9-18 * * *": "每天 09-18 点的每小时",
            "0 0 1 * *": "每月 1 日 00:00",
        }
        return table.get(self._raw, "按表达式 %s 执行" % self._raw)
