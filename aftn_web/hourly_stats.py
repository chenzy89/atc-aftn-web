"""时段流量统计 — 每小时保存扇区/机场架次与使用跑道

数据来源：
- 扇区架次：sector_callsigns_10min（UTC），按小时 6 个 slot 去重航班数
- 机场进出港：flight_plans 的 atd/ata（UTC）落在该小时
- 使用跑道：CAT062 雷达数据实时收集（airport → runway → last_seen），
  每小时快照最近活跃（含 10 分钟容差）的跑道，多个用 '/' 分隔
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger("aftn_web.hourly_stats")

# 关注的机场
WATCH_AIRPORTS = ("ZGSZ", "ZGSD", "VMMC", "ZGNT", "ZGUH")
# 扇区代码 TM01-TM07
SECTOR_CODES = [f"ZGJDTM{i:02d}" for i in range(1, 8)]


class HourlyStatsTracker:
    """跟踪 CAT062 跑道使用 + 计算并保存每小时时段流量"""

    def __init__(self) -> None:
        # airport -> {runway: last_seen_epoch}
        self._runway_usage: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def record_radar(self, parsed: dict[str, Any], received_at: datetime) -> None:
        """从 CAT062 雷达记录中收集跑道使用情况"""
        rw = (parsed.get("runway") or "").strip()
        rw = rw.replace("\\u0000", "").replace("\x00", "").strip()  # 清洗 NUL 转义/非打印字符
        if not rw:
            return
        adep = (parsed.get("adep") or "").strip().upper()
        adest = (parsed.get("adest") or "").strip().upper()
        ts = received_at.timestamp()
        with self._lock:
            for ap in (adep, adest):
                if ap in WATCH_AIRPORTS:
                    self._runway_usage.setdefault(ap, {})[rw] = ts

    def snapshot_runways(self, hour_start: datetime) -> dict[str, str]:
        """快照某小时的使用跑道

        取 last_seen >= 小时开始前 10 分钟（容差覆盖跨时段起降/数据延迟）
        的跑道，按最后活跃时间升序，多个用 '/' 分隔。
        """
        cutoff = (hour_start - timedelta(minutes=10)).timestamp()
        result: dict[str, str] = {}
        with self._lock:
            for ap in WATCH_AIRPORTS:
                usage = self._runway_usage.get(ap, {})
                active = [(rw, ts) for rw, ts in usage.items() if ts >= cutoff]
                active.sort(key=lambda x: x[1])
                result[ap] = "/".join(rw for rw, _ in active)
        return result

    def compute_and_save(self, db: Any, hour_start: datetime) -> dict | None:
        """计算并保存 hour_start 所在小时（UTC）的时段流量，返回统计结果"""
        hour_end = hour_start + timedelta(hours=1)
        date_str = hour_start.strftime("%Y-%m-%d")
        h0 = hour_start.hour
        slot_a, slot_b = h0 * 6, h0 * 6 + 5

        stats: dict = {"hour": hour_start.strftime("%Y-%m-%d %H:00")}

        # TM 扇区架次（去重）
        for i, code in enumerate(SECTOR_CODES, start=1):
            try:
                stats[f"tm{i:02d}"] = db.count_sector_callsigns_hour(
                    code, date_str, slot_a, slot_b)
            except Exception:
                logger.exception("时段流量 TM%02d 统计失败", i)
                stats[f"tm{i:02d}"] = 0

        # 机场进出港
        for ap in ("ZGSZ", "ZGSD", "VMMC"):
            try:
                stats[f"{ap.lower()}_dep"] = db.count_flights_airport_hour(
                    ap, "dep", hour_start, hour_end)
                stats[f"{ap.lower()}_arr"] = db.count_flights_airport_hour(
                    ap, "arr", hour_start, hour_end)
            except Exception:
                logger.exception("时段流量 %s 进出港统计失败", ap)
                stats[f"{ap.lower()}_dep"] = 0
                stats[f"{ap.lower()}_arr"] = 0
        for ap in ("ZGNT", "ZGUH"):
            try:
                stats[ap.lower()] = db.count_flights_airport_hour(
                    ap, "both", hour_start, hour_end)
            except Exception:
                logger.exception("时段流量 %s 统计失败", ap)
                stats[ap.lower()] = 0

        # 使用跑道
        runways = self.snapshot_runways(hour_start)
        stats["zgsz_runway"] = runways.get("ZGSZ", "")
        stats["zgsd_runway"] = runways.get("ZGSD", "")
        stats["vmmc_runway"] = runways.get("VMMC", "")

        try:
            db.save_hourly_traffic(stats)
        except Exception:
            logger.exception("时段流量保存失败: %s", stats["hour"])
            return None
        return stats
