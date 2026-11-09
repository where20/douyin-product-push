#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_repeat.py — 内容重复度体检闸门

扫最近 N 天的 output/*/feed.json, 统计:
  1. 跨天商品复用   —— 同一个商品 id 出现在多天
  2. 文案完全重复   —— 一字不差的文案出现在多天
  3. 首句重复      —— 文案第一句(开头钩子 + 干货句)重复
  4. 当天子类目撞车 —— 一天里出现重复的子类目标签
  5. 干货句单独重复 —— tip 字段本身在多天复用

超阈值 → exit 2(硬失败), 流水线能第一时间发现"内容在复读"。

用法:
    python3 scripts/check_repeat.py                # 扫最近 14 天
    python3 scripts/check_repeat.py --days 30
    python3 scripts/check_repeat.py --date 2026-11-09   # 只看这一天(当天视角)
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "output")

# ⚠️ 2026-11-09 起的新逻辑(tips 扩到 10 条 + {word} 参数化 + 子类目按日期轮转)
#    只在 **LEGACY_UNTIL 之后**的产物上生效。之前的 11 天是旧代码产的,
#    它们天生重复(实测 4 条文案一字不差、首句只有 4 种高频),
#    拿旧数据卡新闸门 → 闸门永远红, 等于没闸门。
#    → 默认只判 LEGACY_UNTIL 之后的日子; 想知道老数据什么样就显式传 --since。
LEGACY_UNTIL = "2026-11-08"

# ---- 阈值 ----
MAX_PRODUCT_REUSE = 0      # 跨天商品复用次数(超出即失败)
MAX_COPY_DUP = 0           # 完全相同文案的出现次数
MAX_FIRST_SENT_DUP = 2     # 同一句开头最多出现几次(允许同一天内部重复)
MAX_LABEL_CLASH = 0        # 一天内允许的子类目撞车次数
MAX_TIP_DUP = 3            # 同一条干货句最多复用几次


def load_days(output_dir=None):
    base = output_dir or OUTPUT
    days = []
    if not os.path.isdir(base):
        return days
    for name in sorted(os.listdir(base)):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            fp = os.path.join(base, name, "feed.json")
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except Exception:
                    continue
                posts = data if isinstance(data, list) else (
                    data.get("posts") or data.get("products") or [])
                if posts:
                    days.append((name, posts))
    return days


def first_sentence(copy: str) -> str:
    """文案第一句 = 开头钩子 + 干货句 (到第一个句号/逗号组结束)。"""
    c = str(copy or "").strip()
    m = re.match(r"^(.*?[。！？])", c)
    if m:
        return m.group(1)
    m = re.match(r"^(.*?，.{0,24})", c)
    return m.group(1) if m else c[:30]


def main():
    ap = argparse.ArgumentParser(description="抖音图文内容重复度体检")
    ap.add_argument("--days", type=int, default=30, help="扫最近多少天(默认 30 = 覆盖整个养号周期)")
    ap.add_argument("--date", default=None, help="只看某一天")
    ap.add_argument("--since", default=LEGACY_UNTIL,
                    help=f"只看这一天之后的(默认 {LEGACY_UNTIL}, 即新逻辑上线日之前的老数据不判)")
    ap.add_argument("--output-dir", default=None, help="扫别的目录(回归模拟用)")
    args = ap.parse_args()

    days = load_days(args.output_dir)
    if not days:
        print("❌ output/ 下没有可分析的 feed.json")
        return 1
    if args.since:
        days = [d for d in days if d[0] > args.since]
    if args.date:
        days = [d for d in days if d[0] == args.date]
        if not days:
            print(f"❌ 没有符合条件的 feed.json (date={args.date} since={args.since})")
            return 1
    elif args.days and len(days) > args.days:
        days = days[-args.days:]

    span = f"{days[0][0]} ~ {days[-1][0]}"
    print(f"🔍 内容重复体检 · {span} · {len(days)} 天 "
          f"· {sum(len(p) for _, p in days)} 条图文\n")

    prod_days = defaultdict(list)
    copy_cnt = Counter()
    copy_first = {}
    first_cnt = Counter()
    tip_cnt = Counter()
    label_clash = []
    problems = []

    for day, posts in days:
        labels = []
        for p in posts:
            pid = str(p.get("id", ""))
            prod_days[pid].append(day)
            cp = str(p.get("copy") or "")
            copy_cnt[cp] += 1
            copy_first.setdefault(cp, day)
            fs = first_sentence(cp)
            first_cnt[fs] += 1
            tp = str(p.get("tip") or "")
            if tp:
                tip_cnt[tp] += 1
            labels.append(p.get("source_label", "") or "")
        dup = [k for k, v in Counter(labels).items() if v > 1]
        if dup:
            label_clash.append((day, dup))

    # 1) 跨天商品复用
    reused = {k: v for k, v in prod_days.items() if len(v) > 1}
    if reused:
        for pid, ds in sorted(reused.items(), key=lambda x: -len(x[1]))[:8]:
            problems.append(f"商品 {pid} 在 {len(ds)} 天重复出现: {', '.join(ds)}")
    print(f"  商品复用      : {len(reused)} 个 (阈值 {MAX_PRODUCT_REUSE})")

    # 2) 文案完全重复
    dup_copies = {c: n for c, n in copy_cnt.items() if n > 1}
    if dup_copies:
        for c, n in sorted(dup_copies.items(), key=lambda x: -x[1])[:5]:
            problems.append(f"文案重复 ×{n} (首现 {copy_first[c]}): {c[:40]}…")
    print(f"  文案完全重复  : {len(dup_copies)} 条 (阈值 {MAX_COPY_DUP})")

    # 3) 首句重复
    hot_first = {s: n for s, n in first_cnt.items() if n > MAX_FIRST_SENT_DUP}
    if hot_first:
        for s, n in sorted(hot_first.items(), key=lambda x: -x[1])[:6]:
            problems.append(f"首句出现 ×{n}: {s[:34]}…")
    print(f"  首句高频      : {len(hot_first)} 句超 {MAX_FIRST_SENT_DUP} 次")

    # 4) 当天子类目撞车
    if label_clash:
        for day, dup in label_clash[:6]:
            problems.append(f"{day} 当天子类目撞车: {', '.join(dup)}")
    print(f"  当天子类目撞车: {len(label_clash)} 天 (阈值 {MAX_LABEL_CLASH})")

    # 5) 干货句复用
    hot_tips = {t: n for t, n in tip_cnt.items() if n > MAX_TIP_DUP}
    if hot_tips:
        for t, n in sorted(hot_tips.items(), key=lambda x: -x[1])[:5]:
            problems.append(f"干货句复用 ×{n}: {t[:34]}…")
    print(f"  干货句复用    : {len(hot_tips)} 句超 {MAX_TIP_DUP} 次")

    print()
    if problems:
        print("❌ 体检不通过:")
        for p in problems:
            print(f"   - {p}")
        return 2
    print("✅ 体检通过: 无重复 / 无撞车")
    return 0


if __name__ == "__main__":
    sys.exit(main())
