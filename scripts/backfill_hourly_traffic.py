#!/usr/bin/env python3
"""回填 hourly_traffic 时段流量（含使用跑道，从 radar_history 重建）

用法:
    python3 scripts/backfill_hourly_traffic.py [db_path] [date_from] [date_to]

示例:
    python3 scripts/backfill_hourly_traffic.py data/aftn.db 2026-06-10 2026-07-31

说明:
- 扇区/机场架次从 DB（sector_callsigns_10min + flight_plans）计算
- 使用跑道从 data/radar_history/radar_YYYYMMDD.jsonl.gz 按时间顺序重建
  （每时段取 last_seen >= 时段开始前 10 分钟的活跃跑道，多个用 '/' 分隔）
- 已存在的记录会更新架次、保留已有跑道（UPSERT 语义）
"""

import gzip
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from __future__ import annotations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aftn_web.database import Database
from aftn_web.hourly_stats import WATCH_AIRPORTS, SECTOR_CODES


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/aftn.db"
    date_from = sys.argv[2] if len(sys.argv) > 2 else "2026-06-10"
    date_to = sys.argv[3] if len(sys.argv) > 3 else "2026-07-31"

    db = Database(db_path)
    rad_dir = Path(db_path).parent / "radar_history"
    if not rad_dir.is_dir():
        print(f"❌ 雷达历史目录不存在: {rad_dir}")
        return 1

    d_from = datetime.strptime(date_from, "%Y-%m-%d")
    d_to = datetime.strptime(date_to, "%Y-%m-%d")

    # 待快照的小时（None 表示还没有上下文）
    pending: datetime | None = None
    runway_usage: dict[str, dict[str, float]] = {}
    saved_hours: set[str] = set()

    def save_hour(h: datetime) -> None:
        """保存 h 所在小时的时段流量（含跑道快照）"""
        stats = {"hour": h.strftime("%Y-%m-%d %H:00")}
        date_str = h.strftime("%Y-%m-%d")
        slot_a, slot_b = h.hour * 6, h.hour * 6 + 5
        for i, code in enumerate(SECTOR_CODES, start=1):
            try:
                stats[f"tm{i:02d}"] = db.count_sector_callsigns_hour(code, date_str, slot_a, slot_b)
            except Exception:
                stats[f"tm{i:02d}"] = 0
        h_end = h + timedelta(hours=1)
        for ap in ("ZGSZ", "ZGSD", "VMMC"):
            try:
                stats[f"{ap.lower()}_dep"] = db.count_flights_airport_hour(ap, "dep", h, h_end)
                stats[f"{ap.lower()}_arr"] = db.count_flights_airport_hour(ap, "arr", h, h_end)
            except Exception:
                stats[f"{ap.lower()}_dep"] = 0
                stats[f"{ap.lower()}_arr"] = 0
        for ap in ("ZGNT", "ZGUH"):
            try:
                stats[ap.lower()] = db.count_flights_airport_hour(ap, "both", h, h_end)
            except Exception:
                stats[ap.lower()] = 0
        cutoff = (h - timedelta(minutes=10)).timestamp()
        for ap in WATCH_AIRPORTS:
            active = [(rw, ts) for rw, ts in runway_usage.get(ap, {}).items() if ts >= cutoff]
            active.sort(key=lambda x: x[1])
            stats[f"{ap.lower()}_runway"] = "/".join(rw for rw, _ in active)
        db.save_hourly_traffic(stats)
        saved_hours.add(stats["hour"])

    # 按时间顺序遍历雷达历史文件，重建跑道使用 + 每小时快照
    # 性能优化：只处理 rw 非空的记录（起降/进近阶段），用快速字段提取替代 json.loads
    total_files = 0
    kept = 0

    def _fields(line: str) -> tuple[str, str, str, str]:
        """快速提取 ts/ap/ad/rw（字段顺序固定，避免 json.loads 开销）"""
        def _get(key: str) -> str:
            i = line.find('"' + key + '":"')
            if i < 0:
                return ""
            j = line.find('"', i + len(key) + 3)
            return line[i + len(key) + 3:j] if j > 0 else ""
        return _get("ts"), _get("ap"), _get("ad"), _get("rw")

    for day in range((d_to - d_from).days + 1):
        day_dt = d_from + timedelta(days=day)
        fname = f"radar_{day_dt.strftime('%Y%m%d')}.jsonl.gz"
        fpath = rad_dir / fname
        if not fpath.exists():
            print(f"⚠ 跳过缺失文件: {fname}")
            continue
        total_files += 1
        with gzip.open(fpath, "rt", encoding="utf-8") as f:
            for line in f:
                if '"rw":""' in line:  # 无跑道记录，跳过（保持时间顺序推进交给有跑道的行）
                    continue
                ts, ap, ad, rw = _fields(line)
                if not ts or not rw:
                    continue
                kept += 1
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
                except ValueError:
                    continue
                hour = ts_dt.replace(minute=0, second=0, microsecond=0)
                if pending is not None and hour > pending:
                    save_hour(pending)
                    pending = hour
                elif pending is None:
                    pending = hour
                tsv = ts_dt.timestamp()
                for ap_code in (ap.upper(), ad.upper()):
                    if ap_code in WATCH_AIRPORTS:
                        runway_usage.setdefault(ap_code, {})[rw] = tsv
    if pending is not None:
        save_hour(pending)

    print(f"✅ 雷达文件处理 {total_files} 个，处理跑道记录 {kept} 条，按时段快照保存 {len(saved_hours)} 小时")

    # 补齐：日期范围内所有缺失小时（无雷达数据的时段也保存架次）
    missing = 0
    cur = d_from
    while cur <= d_to + timedelta(days=1) - timedelta(hours=1):
        key = cur.strftime("%Y-%m-%d %H:00")
        if key not in saved_hours:
            try:
                db.save_hourly_traffic({
                    "hour": key,
                    "tm01": db.count_sector_callsigns_hour("ZGJDTM01", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "tm02": db.count_sector_callsigns_hour("ZGJDTM02", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "tm03": db.count_sector_callsigns_hour("ZGJDTM03", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "tm04": db.count_sector_callsigns_hour("ZGJDTM04", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "tm05": db.count_sector_callsigns_hour("ZGJDTM05", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "tm06": db.count_sector_callsigns_hour("ZGJDTM06", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "tm07": db.count_sector_callsigns_hour("ZGJDTM07", cur.strftime("%Y-%m-%d"), cur.hour * 6, cur.hour * 6 + 5),
                    "zgsz_dep": 0, "zgsz_arr": 0, "zgsz_runway": "",
                    "zgsd_dep": 0, "zgsd_arr": 0, "zgsd_runway": "",
                    "vmmc_dep": 0, "vmmc_arr": 0, "vmmc_runway": "",
                    "zgnt": 0, "zguh": 0,
                })
                missing += 1
            except Exception:
                pass
        cur += timedelta(hours=1)
    print(f"✅ 补齐无雷达时段 {missing} 小时")
    return 0


if __name__ == "__main__":
    sys.exit(main())
