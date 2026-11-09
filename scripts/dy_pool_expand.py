#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dy_pool_expand.py — 找更多家居收纳清洁垂直的京东榜单页

方法: 每个家居榜单页的**导航区**挂着 10 个"相关榜单"链接(带品类名),
      这些链接指向的榜里有一部分也是家居垂直。
      → 顺着 23 个源页的导航爬, 过滤出家居垂直的榜, 追加进 sources.json。

⚠️ 京东抓取的三条铁律(2026-11-09 实测):
   1. 必须带**会话 cookie**(-c/-b 同一个 jar), 否则拿到 8481 字节通用错误页
   2. Referer 用 https://www.jd.com/ 即可(榜单页之间不校验父子关系)
   3. 连续快请求会整段限流 → 源间隔 2.5s, 识别错误页后退避重试

用法:
    python3 scripts/dy_pool_expand.py --scan          # 只扫描, 输出候选
    python3 scripts/dy_pool_expand.py --scan --apply  # 扫描并写入 sources.json
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from lib_dy import POOL_ROOT, read_json, write_json  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
JAR = os.path.join(tempfile.gettempdir(), "dy_expand_cookies.txt")

# 家居收纳清洁垂直的品类词
HOME_WORDS = [
    "收纳", "储物", "整理箱", "置物架", "衣架", "毛巾架", "鞋架", "挂钩", "晾衣",
    "扫把", "扫帚", "笤帚", "簸箕", "撮箕", "畚箕", "拖把", "抹布", "刷子", "海绵",
    "垃圾桶", "垃圾袋", "保鲜", "碗碟", "餐具", "杯子", "水杯", "厨房", "灶台",
    "浴室", "卫浴", "浴室柜", "镜子", "马桶", "坐便器", "浴帘", "花洒", "洗漱",
    "牙具", "漱口", "清洁", "洗衣", "晾衣架", "针线", "剪刀", "胶带", "胶水",
    "五金", "工具", "梯子", "手推车", "藤编", "布艺", "箱包", "抽纸", "纸巾",
    "卷纸", "卫生纸", "衣橱", "衣柜", "桌面", "橱柜", "鞋盒", "内衣", "袜子",
    "壁挂", "免打孔", "家居", "家装", "浴室凳", "泡澡桶", "皂", "香皂", "浴",
]

# 一票否决(不属于家居垂直的品类)
BLOCK_WORDS = [
    "手机", "笔记本", "电脑", "显卡", "相机", "手表", "耳机", "音箱", "平板",
    "电视", "冰箱", "洗衣机", "空调", "汽车", "摩托", "自行车", "奶粉", "零食",
    "面膜", "口红", "香水", "玩具", "童装", "运动", "球", "钓", "药", "显示器",
    "路由器", "键盘", "硬盘", "显卡", "服务器", "显卡", "鼠标", "显卡",
]


def curl(url, referer="https://www.jd.com/"):
    r = subprocess.run(
        ["curl", "--noproxy", "*", "-m", "20", "-s", "-L", "-A", UA,
         "-c", JAR, "-b", JAR, "-H", f"Referer: {referer}", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def title_of(h):
    m = re.search(r"<title>([^<]*)</title>", h)
    return (m.group(1).strip() if m else "")


def nav_links(h):
    """榜单页导航区的榜单链接 → [(锚文本, url)]"""
    out = []
    for m in re.finditer(
            r'<a[^>]*href="(https?://www\.jd\.com/phb/(?:key_)?[a-zA-Z0-9]+\.html)"[^>]*>(.*?)</a>',
            h, re.S):
        txt = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if txt:
            out.append((txt, m.group(1)))
    return out


def product_names(h):
    out = []
    for blk in h.split('<div class="product-img">')[1:]:
        m = re.search(r'alt="([^"]*)"', blk)
        if m:
            out.append(m.group(1).strip())
    return out


def home_ratio(names):
    if not names:
        return 0.0
    return sum(1 for n in names if any(k in n for k in HOME_WORDS)) / len(names)


def clean_label(anchor_title: str) -> str:
    """把榜名去掉品牌前缀 → 中性类目名。

    ⚠️ AGENTS.md 明确要求: 子类目名不能带品牌前缀,
       否则文案里会出现替品牌背书的句子("洁捷升毛巾架"→ 写出去就是给洁捷升打广告)。
    """
    t = re.sub(r"排行榜.*$", "", anchor_title).strip()
    t = re.sub(r"[-_—].*$", "", t).strip()
    # 去掉品牌段: 中文品牌常见是前 2-4 字, 后面跟品类词
    for cut in range(min(4, max(1, len(t) - 2)), 0, -1):
        rest = t[cut:]
        if rest and any(k in rest for k in HOME_WORDS):
            return rest
    return t


def fetch_with_backoff(url, tries=3):
    """限流时退避重试。返回 html 或 None。"""
    for attempt in range(tries):
        h = curl(url)
        if not h:
            return None
        if 'class="product-img"' in h:
            return h
        if len(h) > 200000:      # "京东优评"这类非榜单栏目
            return None
        print(f"      ⚠️ 受限({len(h)}B) → 退避 60s", flush=True)
        time.sleep(60)
    return None


def main():
    ap = argparse.ArgumentParser(description="扩展家居垂直榜单源")
    ap.add_argument("--scan", action="store_true", help="扫描候选")
    ap.add_argument("--apply", action="store_true", help="把候选写进 sources.json")
    ap.add_argument("--max-new", type=int, default=25, help="最多新增多少个源")
    args = ap.parse_args()

    cfg_path = os.path.join(POOL_ROOT, "sources.json")
    cfg = read_json(cfg_path)
    sources = cfg.get("sources") or []
    known = {s.get("url") for s in sources}
    print(f"📦 现有源 {len(sources)} 个\n")

    # ---- 1. 扫现有源页的导航, 收集候选榜 ----
    candidates = {}
    for i, s in enumerate(sources, 1):
        url = s.get("url", "")
        print(f"  [{i}/{len(sources)}] 扫 {s.get('label','')} …", flush=True)
        h = curl(url)
        if 'class="product-img"' not in h:
            print(f"      ⚠️ 取不到(可能被限流)")
            time.sleep(3)
            continue
        for txt, u in nav_links(h):
            if u in known:
                continue
            if any(b in txt for b in BLOCK_WORDS):
                continue
            if not any(w in txt for w in HOME_WORDS):
                continue
            candidates.setdefault(u, txt)
        time.sleep(2.5)

    print(f"\n🔎 导航里筛出 {len(candidates)} 个家居向候选, 开始验证 …\n")

    # ---- 2. 逐个验证(看真实商品, 不靠榜名猜) ----
    verified = []
    for u, txt in candidates.items():
        h = fetch_with_backoff(u)
        time.sleep(2.5)
        if h is None:
            continue
        t = title_of(h)
        names = product_names(h)
        hr = home_ratio(names)
        if hr < 0.5:
            continue
        if any(b in t for b in BLOCK_WORDS):
            continue
        label = clean_label(txt)
        if not label or len(label) > 12:
            label = clean_label(t)
        verified.append({"url": u, "label": label, "title": t,
                         "count": len(names), "home_ratio": round(hr, 2),
                         "names": names[:6]})
        print(f"   ✅ {t[:30]:<32} {len(names):>3} 商品 家居{hr:>4.0%}  → label「{label}」",
              flush=True)

    print(f"\n📦 验证通过 {len(verified)} 个")
    out_path = os.path.join(POOL_ROOT, "candidates.json")
    write_json(out_path, verified)
    print(f"   候选清单: {out_path}")

    if not args.apply:
        print("\n(加 --apply 才会写进 sources.json)")
        return 0

    # ---- 3. 写进 sources.json (tips/product_en 需要人工补, 先留占位) ----
    added = 0
    for v in verified:
        if added >= args.max_new:
            break
        if any(s.get("url") == v["url"] for s in sources):
            continue
        label = v["label"]
        sources.append({
            "label": label,
            "url": v["url"],
            "keywords": [label],
            "tips": [],          # ← 必须人工补, 否则文案没有干货句
            "product_en": "",
        })
        added += 1
    cfg["sources"] = sources
    write_json(cfg_path, cfg)
    print(f"✅ 新增 {added} 个源到 sources.json")
    if added:
        print("⚠️ 新源的 tips 是空的 —— 必须补上干货句, 否则文案会退化成通用句")
    return 0


if __name__ == "__main__":
    sys.exit(main())
