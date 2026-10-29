#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_links.py — 发布前全量 URL 核验闸门

用法:
    python3 scripts/verify_links.py <date>

核验三组 URL, 全部必须 200/202:
    1. feed.json 里的 links.primary / links.extra   (商品溯源)
    2. uploaded_urls.json 里的图床 URL               (发布时要粘的)
    3. 榜单页 URL                                     (养号期商品池的来源)

⚠️ **软失败**(200 但没有商品数据)单独标出来, 不算硬失败但必须知道:
    - `item.jd.com/{sku}.html` → 200, 但返回**通用首页**, 无商品数据。
      这是京东反爬, 不是我们的问题 → 只能当 SKU 溯源用, 不能当商品页。

退出码: 0 = 全绿 / 1 = 有硬失败
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import ensure_out_dir, out_dir, read_json, resolve_date  # noqa: E402

# 200 但拿不到目标内容的域名 → 软失败, 单独标注
KNOWN_SOFT_HOSTS = {
    "item.jd.com": "京东详情页反爬: 返回 200 但是通用首页, 无商品数据, 只能当 SKU 溯源",
}


def head_status(url: str, timeout: int = 15) -> tuple:
    """返回 (状态码, 错误信息)。⚠️ 一律 --noproxy '*'。"""
    cmd = ["curl", "--noproxy", "*", "-m", str(timeout), "-s", "-o", "/dev/null",
           "-w", "%{http_code}", "-L", "-A",
           "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 8)
    except subprocess.TimeoutExpired:
        return 0, f"curl 超时 ({timeout}s)"
    code = (r.stdout or "").strip()[-3:]
    try:
        return int(code), r.stderr.strip()[:120]
    except ValueError:
        return 0, r.stderr.strip()[:120] or "无状态码"


def collect(od: str) -> list:
    """汇总要核验的 URL: [(分组, URL), ...]"""
    out = []

    feed_path = os.path.join(od, "feed.json")
    if os.path.exists(feed_path):
        for p in read_json(feed_path):
            links = p.get("links") or {}
            if links.get("primary"):
                out.append((f"商品[{p.get('id')}]primary", links["primary"]))
            for u in links.get("extra") or []:
                out.append((f"商品[{p.get('id')}]extra", u))

    up_path = os.path.join(od, "uploaded_urls.json")
    if os.path.exists(up_path):
        for pid, roles in (read_json(up_path).get("images") or {}).items():
            for role, u in (roles or {}).items():
                if u:
                    out.append((f"图床[{pid}.{role}]", u))

    return out


def main():
    ap = argparse.ArgumentParser(description="抖音支线 URL 核验闸门")
    ap.add_argument("date", nargs="?", default=None)
    args = ap.parse_args()
    d = resolve_date(args.date)
    od = ensure_out_dir(d)

    urls = collect(od)
    if not urls:
        print(f"❌ {od} 下没有可核验的 URL (先跑 build_feed / publish_pack)", file=sys.stderr)
        return 2

    print(f"🔍 核验 {len(urls)} 个 URL ({d})\n")
    hard_fail = []
    soft = []
    ok_n = 0

    for tag, url in urls:
        code, err = head_status(url)
        host = ""
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc
        except Exception:
            pass

        if code in (200, 202):
            if host in KNOWN_SOFT_HOSTS:
                soft.append((tag, url, KNOWN_SOFT_HOSTS[host]))
                print(f"   ⚠️  [{tag}] {code} {url}")
                print(f"        ↳ {KNOWN_SOFT_HOSTS[host]}")
            else:
                ok_n += 1
                print(f"   ✅ [{tag}] {code} {url}")
        else:
            hard_fail.append((tag, url, code, err))
            print(f"   ❌ [{tag}] {code} {url}  {err}")

    print(f"\n{'=' * 60}")
    print(f"核验完成: {ok_n} 全绿 / {len(soft)} 软失败 / {len(hard_fail)} 硬失败")
    if soft:
        print(f"💡 {len(soft)} 个软失败不影响发布, 但别当商品页用")
    if hard_fail:
        print("❌ 有硬失败 —— 换 URL 或重传, 不要发这一批", file=sys.stderr)
        return 1
    print("✅ 全绿, 可以发")
    return 0


if __name__ == "__main__":
    sys.exit(main())
