#!/usr/bin/env python3
"""批量回补离港航班的终端区进出时间

问题说明：v2.2.13 之前，离港航班第一次雷达回波（<30m）被误判为
"落地"，导致 entry_time 为空、exit_time 错设为第一次雷达时间。
此脚本利用 flight_tracks 中的航迹点，反算正确的进出时间。

用法：
  python3 scripts/backfill_terminal_times.py [--date YYYY-MM-DD] [--dry-run]
  --date: 指定回补日期，不指定则回补所有受影响日期
  --dry-run: 仅显示要更新的记录，不修改数据库
"""

import json
import sys
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

# ── 确保能找到项目模块 ──────────────────────────────
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_DIR)

from aftn_web.terminal_area import is_in_terminal, load_terminal_config, reload_terminal_config
from aftn_web.database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill")


def parse_ago():
    """解析命令行参数"""
    import argparse
    ap = argparse.ArgumentParser(description="批量回补离港航班终端区进出时间")
    ap.add_argument("--date", help="回补指定日期 YYYY-MM-DD（默认回补所有受影响日期）")
    ap.add_argument("--dry-run", action="store_true", help="仅显示，不改数据库")
    return ap.parse_args()


def find_affected_flights(db: Database, target_date: str = "") -> List[Dict]:
    """找出所有需要回补的离港航班
    
    条件：
    - 起飞机场属于终端区机场
    - entry_time 为空
    - exit_time 非空（表明 FDR 有处理过但日期错误）
    - atd 非空（确认为离港）
    - 有时间范围限制
    """
    airport_list = ["ZGSZ", "ZGSD", "VMMC", "ZGNT", "ZGUH", "ZGHZ"]
    adep_placeholders = ",".join("?" for _ in airport_list)
    
    date_filter = ""
    params = list(airport_list)
    if target_date:
        date_filter = "AND dof=?"
        params.append(target_date)

    conn = db._get_conn()
    rows = conn.execute(
        f"""SELECT id, callsign, adep, adest, dof, atd, entry_time, exit_time, terminal_flight_time
            FROM flight_plans
            WHERE adep IN ({adep_placeholders})
              AND entry_time=''
              AND exit_time!=''
              AND atd!=''
              {date_filter}
            ORDER BY id""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def compute_terminal_times(track_points: List[Dict]) -> Optional[dict]:
    """从航迹点计算终端进出时间
    
    解析所有航迹点，找首次进终端和最后出终端的时间。
    返回 {"entry_ts": str, "exit_ts": str, "flight_time_s": int} 或 None
    """
    if len(track_points) < 3:
        return None

    reload_terminal_config()
    entry_ts = ""
    exit_ts = ""
    prev_in = False

    for pt in track_points:
        lat = pt.get("lt", 0.0)
        lon = pt.get("ln", 0.0)
        alt = pt.get("fl", 0.0)
        ts = pt.get("ts", "")

        if not ts or not lat or not lon:
            continue

        # 高度 > 0 时才检测（和 FDR 逻辑一致）
        in_term = is_in_terminal(lat, lon, alt) if alt > 0 else False

        if in_term and not entry_ts:
            # 首次进终端
            entry_ts = ts
        elif not in_term and entry_ts and prev_in:
            # 出终端（从终端内到外）
            exit_ts = ts

        prev_in = in_term

    if not entry_ts:
        return None

    return {
        "entry_ts": entry_ts,
        "exit_ts": exit_ts,
        "flight_time_s": _time_diff_s(entry_ts, exit_ts) if exit_ts else 0,
    }


def _time_diff_s(t1: str, t2: str) -> int:
    """计算两个 ISO 时间戳的秒数差"""
    try:
        dt1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
        dt2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        return max(0, int(abs((dt2 - dt1).total_seconds())))
    except (ValueError, TypeError):
        return 0


def _fmt_ts(ts: str) -> str:
    """格式化时间戳用于显示"""
    if not ts:
        return "-"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return ts[:19]


def backfill_departure(db: Database, flight: dict, track_points: List[Dict],
                        dry_run: bool = False) -> bool:
    """回补单个离港航班
    
    返回 True 表示成功或有更新
    """
    result = compute_terminal_times(track_points)
    if not result:
        return False

    entry_ts = result["entry_ts"]
    exit_ts = result["exit_ts"]
    flight_time = result["flight_time_s"]

    # 已有值
    old_entry = flight.get("entry_time", "")
    old_exit = flight.get("exit_time", "")
    old_tft = flight.get("terminal_flight_time", 0) or 0

    # 如果现有 entry 非空且更早（更准确），跳过
    if old_entry and old_entry <= entry_ts:
        logger.debug("  ⇢ 已有更早的 entry_time=%s，跳过", old_entry[:19])
        return False

    # 只有 entry 更新了才算有效修复
    if not entry_ts:
        return False

    if dry_run:
        logger.info(
            "  [预演] #%d %s %s->%s entry=%s exit=%s tft=%ds (原: entry=- exit=%s tft=%d)",
            flight["id"], flight["callsign"], flight["adep"], flight["adest"],
            _fmt_ts(entry_ts), _fmt_ts(exit_ts), flight_time,
            _fmt_ts(old_exit), old_tft,
        )
        return True

    conn = db._get_conn()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # exit_time 只允许更晚（和 FDR 写入逻辑一致）
    final_exit = exit_ts if (exit_ts and (not old_exit or exit_ts > old_exit)) else old_exit
    # terminal_flight_time 用 entry→exit 跨度（取大值，单调性保护）
    final_tft = max(flight_time, old_tft)

    conn.execute(
        """UPDATE flight_plans SET
            entry_time=?,
            exit_time=?,
            terminal_flight_time=?,
            updated_at=?
           WHERE id=?
           AND (entry_time='' OR ?<entry_time)""",
        (entry_ts, final_exit, final_tft, now, flight["id"], entry_ts),
    )
    conn.commit()

    affected = conn.execute("SELECT changes()").fetchone()[0]
    if affected:
        logger.info(
            "  ✓ #%d %s %s->%s entry=%s exit=%s tft=%ds (原: entry=- exit=%s tft=%d)",
            flight["id"], flight["callsign"], flight["adep"], flight["adest"],
            _fmt_ts(entry_ts), _fmt_ts(final_exit), final_tft,
            _fmt_ts(old_exit), old_tft,
        )
        return True
    return False


def main():
    args = parse_ago()
    dry_run = args.dry_run
    target_date = args.date

    db = Database(os.path.join(REPO_DIR, "data/aftn.db"))

    flights = find_affected_flights(db, target_date)
    total = len(flights)
    if total == 0:
        logger.info("没有需要回补的离港航班")
        return

    logger.info("找到 %d 个需要回补的离港航班%s", total,
                f"（日期：{target_date}）" if target_date else "（全部）")

    if dry_run:
        logger.info("=== 预演模式，不会修改数据库 ===\n")

    fixed = 0
    no_track = 0
    no_result = 0

    conn = db._get_conn()
    for i, flight in enumerate(flights):
        callsign = flight["callsign"]
        dof = flight["dof"]
        logger.debug(f"[{i+1}/{total}] {callsign} {dof}...")

        # 从 flight_tracks 表中取航迹
        track_row = conn.execute(
            "SELECT points_json FROM flight_tracks WHERE callsign=? AND dof=? AND track_type='DEPARTURE' ORDER BY id DESC LIMIT 1",
            (callsign, dof),
        ).fetchone()

        if not track_row:
            no_track += 1
            logger.debug("  ⇢ 无航迹记录，跳过")
            continue

        try:
            points = json.loads(track_row["points_json"])
        except (json.JSONDecodeError, TypeError):
            no_track += 1
            continue

        if backfill_departure(db, flight, points, dry_run=dry_run):
            fixed += 1
        else:
            no_result += 1

    # 汇总
    logger.info(f"\n=== 回补完成 ===")
    logger.info(f"总扫描: {total}")
    logger.info(f"已回补: {fixed}")
    logger.info(f"无航迹: {no_track}")
    logger.info(f"计算失败: {no_result}")
    logger.info(f"未处理: {total - fixed - no_track - no_result}")


if __name__ == "__main__":
    main()
