#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dy_pool.py — 养号期**垂直商品池**构建器

用法:
    python3 scripts/dy_pool.py                       # 用 pool/sources.json 里的全部源
    python3 scripts/dy_pool.py --source <name>       # 只抓某一个源

产出 (pool/{slug}.json):
    {
      "vertical": "家居收纳清洁",
      "category": "家居日用 · 收纳清洁",
      "fetched_at": "2026-10-29",
      "products": [
        {"id","name","sku","rank","reviews","reviews_text","image",
         "color","category","source_label","links":{"primary":...}}
      ]
    }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 为什么养号期要单独建"垂直池"(2026-10-29 踩坑后加的)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
抖音养号的核心不是"发得多", 是**账号标签要准**。系统靠内容判定你是谁,
发得杂 → 标签散 → 推荐池不准 → 涨粉慢 → 图文挂车永远不解锁。

原来 dy_api.py --mock 的 5 个商品跨 5 个类目(日用百货/家用电器/文具办公/
汽车用品/玩具乐器), 拿来直接发 4 天, 账号标签当场就散了 —— 养号白养。

→ 所以养号期从一个**单一垂直**的商品池里出稿, 每天 5 条全是同一个类目。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 数据来源与真实性(别改这条)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
池子数据来自**京东榜单页** `www.jd.com/phb/*.html`(服务端直出, 实测稳定):

  ✅ 真实商品名 / SKU / 评论数 / 主图 / 榜单名次
  ❌ **没有价格**(榜单页 price-rmb 恒空, JS 渲染抓不到)
  ❌ **没有详情描述**(desc 要 100+ 字, 榜单页不提供)

所以:
  - 池子里的商品**不带价格**, 带货期( monetize)用不了池子, 仍走 dy_api.py --mock
  - 池子里的商品**不带 desc**, 养号文案改用"榜单事实"(名次/评论数/规格)来写,
    每一句都是榜单页上的真实数字, 不编造

⚠️ **榜单页内容会按关键词轮换** —— URL 名义上是"抽纸榜"实际可能返回别的品类。
   所以抓完必须逐条 read 真实商品名, 不要凭 URL 猜。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import POOL_ROOT, read_json, write_json  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BLOCK_SPLIT = '<div class="product-img">'


def curl(url: str, timeout: int = 25) -> str:
    """抓 URL。⚠️ 强制 --noproxy '*': 本机代理端口时活时死, 且部分域名被 DNS 劫持。"""
    r = subprocess.run(
        ["curl", "--noproxy", "*", "-m", str(timeout), "-s", "-L", "-A", UA,
         "-H", "Referer: https://www.jd.com/", url],
        capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def fmt_reviews(raw: str) -> str:
    """榜单页评论数 → 可读文案。

    ⚠️ **不夸大**: 原文 "5000000人评论" → "500万条"(整除才换算),
       "61人评论" → "61条"。不四舍五入往上取。
    """
    m = re.search(r"([\d.]+)\s*(万)?", raw or "")
    if not m:
        return ""
    num = float(m.group(1))
    if m.group(2) == "万":
        return f"{num:g}万条"
    if num >= 10000 and num % 10000 == 0:
        return f"{int(num / 10000)}万条"
    return f"{int(num)}条"


def dominant_color(image_url: str) -> str:
    """从**真实主图**取商品主色。

    ⚠️ 2026-10-26 上游踩过: 卡片写黑、图是白 = 图文不符 = 假带货。
       颜色不能猜。这里下载真实主图, 只取中心区域(商品所在处)做量化,
       再在候选色里挑**饱和度最高**的那个, 跳过近白/近黑背景色。

    ⚠️ **失败必须响亮**: 2026-10-29 实测踩到 —— Pillow 12 把量化常量挪到了
       `Image.Quantize.MEDIANCUT`, 老路径 `Image.quantize.MEDIANCUT` 抛
       AttributeError, 又被 except 吞掉 → 44 个商品颜色**全部**静默回落成
       默认值, 而日志里一行报错都没有。颜色错 = 图文不符 = 假带货。
       → 所以这里失败时直接 raise, 不做静默兜底。
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f"缺少 Pillow, 无法取商品主色: {e}")

    tmp = os.path.join(tempfile.gettempdir(), f"dy_pool_{os.getpid()}.jpg")
    try:
        r = subprocess.run(["curl", "--noproxy", "*", "-m", "20", "-s", "-o", tmp,
                            image_url], capture_output=True)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError(f"主图下载失败(curl exit {r.returncode}): {image_url}")
        if os.path.getsize(tmp) < 1000:
            raise RuntimeError(f"主图过小, 疑似下载失败: {image_url}")
        im = Image.open(tmp).convert("RGB")
        w, h = im.size
        # 只取中心 60% —— 商品主体基本都在中间, 四周是白底
        im = im.crop((int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)))
        im = im.resize((64, 64), Image.NEAREST)
        # ⚠️ Pillow >= 10 常量在 Image.Quantize, 老版本在 Image.quantize —— 两个都试
        method = getattr(getattr(Image, "Quantize", Image), "MEDIANCUT", 0)
        q = im.quantize(colors=6, method=method)
        pal = q.getpalette()
        counts = sorted(q.getcolors(), reverse=True)
        best, best_score = None, -1.0
        for count, idx in counts:
            rgb = tuple(pal[idx * 3: idx * 3 + 3])
            r_, g_, b_ = rgb
            mx, mn = max(rgb), min(rgb)
            if mx > 235 and mn > 225:      # 近白 = 背景, 跳过
                continue
            if mx < 28:                  # 近黑 = 阴影/暗部, 跳过
                continue
            sat = (mx - mn) / mx
            score = count * (0.35 + sat)
            if score > best_score:
                best_score, best = score, rgb
        if not best:
            raise RuntimeError(f"主图里找不到可用颜色(全近白/近黑): {image_url}")
        return "#%02X%02X%02X" % best
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def parse_page(url: str, label: str, category: str) -> list:
    """解析一个榜单页 → 商品列表。"""
    html = curl(url)
    if not html:
        print(f"   ❌ 抓取失败: {url}")
        return []
    title_m = re.search(r"<title>([^<]*)</title>", html)
    title = title_m.group(1) if title_m else ""
    products = []
    for block in html.split(BLOCK_SPLIT)[1:]:
        m = re.search(r'<img\s+src="([^"]+)"\s+alt="([^"]*)"', block)
        if not m:
            m = re.search(r'<img[^>]*src="([^"]+)"[^>]*alt="([^"]*)"', block)
        if not m:
            continue
        image, name = m.group(1), m.group(2).strip()
        if image.startswith("//"):
            image = "https:" + image
        sku_m = re.search(r"item\.jd\.com/(\d+)\.html", block)
        if not sku_m:
            continue
        sku = sku_m.group(1)
        # ⚠️ 原文是 "已有5000000人评论", 数字是**原始条数**不是"5万", 别加单位
        cm = re.search(r"已有\s*([\d.]+)\s*(万)?人评论", block)
        if cm:
            reviews_raw = f"{cm.group(1)}{cm.group(2) or ''}人评论"
        else:
            cm2 = re.search(r"([\d.]+万?)\s*条评价", block)
            reviews_raw = f"{cm2.group(1)}条评价" if cm2 else ""
        products.append({
            "id": sku,
            "name": name,
            "sku": sku,
            # ⚠️ rank 必须是**该榜单页里的真实名次**, 不是整个池子的序号。
            #    池子序号写进文案会变成"排第30名"这种假榜单名次。
            "rank": len(products) + 1,
            "reviews_raw": reviews_raw,
            "reviews_text": fmt_reviews(reviews_raw),
            "image": image,
            "source_label": label,
            "source_title": title,
            "category": category,
            # ⚠️ 京东商品直链(item.jd.com/{sku}.html)实测返回通用首页, 抓不到商品;
            #    榜单页是服务端直出、能核验 200 的真实页面 → 当主链。
            "links": {
                "primary": url,
                "source": "京东榜单页",
                "extra": [f"https://item.jd.com/{sku}.html"],
            },
        })
    print(f"   ✅ {label} [{title[:34]}] → {len(products)} 个商品")
    return products


def main():
    ap = argparse.ArgumentParser(description="养号期垂直商品池构建器")
    ap.add_argument("--source", default=None, help="只抓指定源(名字匹配 sources.json)")
    ap.add_argument("--config", default=None, help="源配置 json (默认 pool/sources.json)")
    args = ap.parse_args()

    cfg_path = args.config or os.path.join(POOL_ROOT, "sources.json")
    if not os.path.exists(cfg_path):
        print(f"❌ 找不到源配置 {cfg_path}", file=sys.stderr)
        return 2
    cfg = read_json(cfg_path)
    sources = cfg.get("sources") or []
    if args.source:
        sources = [s for s in sources if args.source in (s.get("label"), s.get("url"))]
    if not sources:
        print("❌ 没有可用的源", file=sys.stderr)
        return 2

    vertical = cfg.get("vertical", "未命名垂直")
    category = cfg.get("category", vertical)
    print(f"📦 垂直: {vertical}  ({len(sources)} 个榜单页)\n")

    products = []
    for s in sources:
        products.extend(parse_page(s["url"], s.get("label", ""), category))

    # 去重(同一个 sku 可能出现在多个榜单页)
    seen, uniq = set(), []
    for p in products:
        if p["sku"] in seen:
            continue
        seen.add(p["sku"])
        uniq.append(p)

    # ⚠️ 按源**轮转**排列, 不要一个子类目连着排。
    #    连着排的话, 每天取的 5 个全是收纳箱 → 连续 9 天都在讲收纳箱,
    #    子话题太单一, 涨粉效率上不去。轮转后每天 5 条覆盖 5 个子类目,
    #    大垂直不变(仍是家居收纳清洁), 但内容不重复。
    by_src = {}
    for p in uniq:
        by_src.setdefault(p["source_label"], []).append(p)
    interleaved = []
    while any(by_src.values()):
        for label in list(by_src):
            if by_src[label]:
                interleaved.append(by_src[label].pop(0))
    uniq = interleaved

    print(f"\n🎨 取主色 ({len(uniq)} 个) ...")
    color_fail = []
    for i, p in enumerate(uniq, 1):
        try:
            p["color"] = dominant_color(p["image"])
            print(f"   [{i:2d}] {p['color']}  {p['name'][:40]}")
        except Exception as e:
            # ⚠️ 不静默兜底: 取色失败就是图文不符的种子, 必须留在日志里
            color_fail.append((p["name"], str(e)))
            p["color"] = None
            print(f"   [{i:2d}] ❌ 取色失败: {e}")

    out = {
        "vertical": vertical,
        "category": category,
        "fetched_at": date.today().strftime("%Y-%m-%d"),
        "count": len(uniq),
        "color_failed": len(color_fail),
        "products": uniq,
    }
    slug = cfg.get("slug") or "pool"
    os.makedirs(POOL_ROOT, exist_ok=True)
    out_path = os.path.join(POOL_ROOT, f"{slug}.json")
    write_json(out_path, out)
    print(f"\n💾 写入 {out_path} ({len(uniq)} 个商品, 单垂直)")
    if color_fail:
        print(f"\n❌ {len(color_fail)} 个商品取色失败 —— 这些商品不能出图(颜色缺失=图文不符)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
