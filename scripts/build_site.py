#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_site.py — 抖音图文落地页 · 数据生成

从 output/{date}/publish_manifest.json + feed.json 生成站点数据:
    site/data/history.json   日期索引 (新 → 旧)
    site/data/{date}.json    每天一档的图文明细

图片直接引用图床真实 URL (uploaded_urls.json 已由 publish_pack 落盘),
不把 jpg 拷进站点 —— 体积小、且图床 URL 每日由 verify_links 核验存活。

用法:
    python3 scripts/build_site.py            # 全量重建
    python3 scripts/build_site.py --check    # 只体检不写文件
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "output")
SITE_DIR = os.path.join(ROOT, "site")
DATA_DIR = os.path.join(SITE_DIR, "data")

# 步骤里带 markdown 强调符号, 渲染成纯文本前先剥掉
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _plain(text: str) -> str:
    return _MD_BOLD.sub(r"\1", text or "").strip()


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️  读不了 {os.path.relpath(path, ROOT)}: {exc}")
        return None


def _feed_colors(date: str) -> dict:
    """feed.json 里有商品主色 color, 给卡片做点缀色 (manifest 里没有)。"""
    feed = _load_json(os.path.join(OUTPUT_DIR, date, "feed.json"))
    out = {}
    if not feed:
        return out
    # feed.json 历史上是 list, 新版可能是 {"posts": [...]}
    if isinstance(feed, dict):
        posts = feed.get("posts") or feed.get("products") or []
    else:
        posts = feed
    if isinstance(posts, dict):
        posts = list(posts.values())
    for item in posts or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id", ""))
        if pid:
            out[pid] = item.get("color") or ""
    return out


def _collect_dates() -> list:
    if not os.path.isdir(OUTPUT_DIR):
        return []
    dates = []
    for name in os.listdir(OUTPUT_DIR):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            continue
        if os.path.isfile(os.path.join(OUTPUT_DIR, name, "publish_manifest.json")):
            dates.append(name)
    return sorted(dates, reverse=True)


def _build_day(date: str) -> dict:
    manifest = _load_json(os.path.join(OUTPUT_DIR, date, "publish_manifest.json"))
    if not manifest:
        return {}
    colors = _feed_colors(date)
    posts = []
    for post in manifest.get("posts") or []:
        posting = post.get("posting") or {}
        links = post.get("links") or {}
        pid = str(post.get("id", ""))
        stage = post.get("stage") or ""
        price_display = post.get("price") if stage == "monetize" else None
        posts.append({
            "id": pid,
            "display_name": post.get("display_name") or post.get("name") or pid,
            "name": post.get("name") or "",
            "category": post.get("category") or "",
            "fact_line": post.get("fact_line") or "",
            "color": colors.get(pid, ""),
            "copy": post.get("copy") or "",
            "tail_copy": post.get("fact_line") or "",
            "images": list(post.get("images") or []),
            "image_roles": list(post.get("image_roles") or []),
            "links": {
                "primary": links.get("primary") or "",
                "source": links.get("source") or "",
                "extra": list(links.get("extra") or []),
            },
            "steps": [_plain(s) for s in (posting.get("steps") or [])],
            "suggested_times": list(posting.get("suggested_times") or []),
            "need_ai_declaration": bool(posting.get("need_ai_declaration")),
            "can_cart": bool(posting.get("can_cart")),
            "stage": stage,
            "price_display": price_display,
        })
    return {
        "date": date,
        "stage": posts[0]["stage"] if posts else "",
        "count": len(posts),
        "posts": posts,
    }


def build(check_only: bool = False) -> int:
    dates = _collect_dates()
    if not dates:
        print("❌ output/ 下没有任何带 publish_manifest.json 的日期目录")
        return 1
    if not check_only:
        os.makedirs(DATA_DIR, exist_ok=True)

    index = []
    problems = []
    for date in dates:
        day = _build_day(date)
        if not day:
            problems.append(f"{date}: 清单读不出来")
            continue
        if day["count"] == 0:
            problems.append(f"{date}: 0 组图文")
        for post in day["posts"]:
            if len(post["images"]) != 3:
                problems.append(f"{date}/{post['id']}: 图片数 {len(post['images'])} ≠ 3")
            if not post["copy"]:
                problems.append(f"{date}/{post['id']}: 文案为空")
            if post["stage"] == "warmup" and post["price_display"]:
                problems.append(f"{date}/{post['id']}: 养号期不该出现价格")
        if not check_only:
            with open(os.path.join(DATA_DIR, f"{date}.json"), "w", encoding="utf-8") as fh:
                json.dump(day, fh, ensure_ascii=False, indent=1)
        index.append({
            "date": date,
            "count": day["count"],
            "stage": day["stage"],
            "image_count": sum(len(p["images"]) for p in day["posts"]),
        })
        print(f"  ✅ {date}  {day['count']} 组 / {sum(len(p['images']) for p in day['posts'])} 图")

    history = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest": dates[0],
        "total_days": len(index),
        "total_posts": sum(i["count"] for i in index),
        "total_images": sum(i["image_count"] for i in index),
        "stage": index[0]["stage"] if index else "",
        "dates": index,
    }
    if not check_only:
        with open(os.path.join(DATA_DIR, "history.json"), "w", encoding="utf-8") as fh:
            json.dump(history, fh, ensure_ascii=False, indent=1)
        print(f"\n📦 站点数据: {os.path.relpath(DATA_DIR, ROOT)}")
    print(f"   {history['total_days']} 档 / {history['total_posts']} 组 / {history['total_images']} 图")

    if problems:
        print("\n⚠️  数据体检发现问题:")
        for p in problems:
            print(f"   - {p}")
        return 2
    print("✅ 数据体检全过")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="生成抖音图文落地页数据")
    ap.add_argument("--check", action="store_true", help="只体检不写文件")
    args = ap.parse_args()
    sys.exit(build(check_only=args.check))
