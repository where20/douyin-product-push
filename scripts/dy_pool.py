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
import time
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (POOL_ROOT, analyze_product_color,  # noqa: E402
                     detect_material, detect_name_color, read_json, write_json)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

BLOCK_SPLIT = '<div class="product-img">'

# ⚠️ 2026-11-09 实测: 京东榜单页需要**会话 cookie**。
#    第一次请求种下 th/session, 后续请求必须带上, 否则拿到的是通用错误页
#    (8481 字节, 标题只有"京东", 0 个商品)。原来每次 curl 都是无状态的,
#    所以多源抓取时后面的源经常整片失败 —— 池子长不大就是这么来的。
_COOKIE_JAR = os.path.join(tempfile.gettempdir(), "dy_pool_cookies.txt")

# ⚠️ 京东对连续快请求会整段返回 8481 字节通用错误页, 歇 1-2 分钟自动恢复。
#    → 源之间限速 + 识别错误页 + 退避重试, 别把失败当"这个源没有商品"。
_SOURCE_GAP = 1.5
_RETRY_SLEEP = 60


def curl(url: str, timeout: int = 25) -> str:
    """抓 URL。⚠️ 强制 --noproxy '*': 本机代理端口时活时死, 且部分域名被 DNS 劫持。

    ⚠️ 带 cookie jar: 榜单页要会话, 否则第二个源开始就全是空页。
    ⚠️ 遇到通用错误页(被限流)要退避重试, 不能当成"这个源没商品"。
    """
    html = ""
    for attempt in range(3):
        r = subprocess.run(
            ["curl", "--noproxy", "*", "-m", str(timeout), "-s", "-L", "-A", UA,
             "-c", _COOKIE_JAR, "-b", _COOKIE_JAR,
             "-H", "Referer: https://www.jd.com/", url],
            capture_output=True)
        html = r.stdout.decode("utf-8", "ignore")
        if not html:
            return html
        # 有商品块 → 正常; 页面够大 → 不是错误页(可能是"优评"这类非榜单栏目)
        if BLOCK_SPLIT in html or len(html) > 20000:
            return html
        print(f"   ⚠️ 被限流(通用错误页 {len(html)}B) → 退避 {_RETRY_SLEEP}s 重试")
        time.sleep(_RETRY_SLEEP)
    return html


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


def _full_res_url(image_url: str) -> str:
    """把榜单页的 80x80 **缩略图** URL 换成大图。

    ⚠️ 2026-11-05 实测踩到: 榜单页 img src 是 `n7/s80x80_jfs/...`,
       只有 80x80 —— 这么小的图做中心裁切量化, 得到的"主色"基本是噪声
       (5 组实测 4 组图文不符)。京东同一张图支持 `s800x800_` 大图,
       同一个 jfs 路径换尺寸即可, 不需要别的接口。
    """
    u = str(image_url or "")
    if "/s80x80_" in u:
        return u.replace("/s80x80_", "/s800x800_")
    return u


def dominant_color(image_url: str, name: str = "") -> str:
    """从**真实主图**取商品主色。

    ⚠️ 2026-10-26 上游踩过: 卡片写黑、图是白 = 图文不符 = 假带货。
       颜色不能猜。这里下载真实主图, 只取中心区域(商品所在处)做量化,
       再在候选色里挑**饱和度最高**的那个, 跳过近白/近黑背景色。

    ⚠️ **失败必须响亮**: 2026-10-29 实测踩到 —— Pillow 12 把量化常量挪到了
       `Image.Quantize.MEDIANCUT`, 老路径 `Image.quantize.MEDIANCUT` 抛
       AttributeError, 又被 except 吞掉 → 44 个商品颜色**全部**静默回落成
       默认值, 而日志里一行报错都没有。颜色错 = 图文不符 = 假带货。
       → 所以这里失败时直接 raise, 不做静默兜底。

    ⚠️ 2026-11-05 重写(根因修复, 11-02 / 11-04 已两次记录):
       老逻辑 = "中心 60% 裁切 + 取像素最多的合格色"。
       但京东榜单主图是**营销合成图**(商品 + 场景 + 价格角标 + 促销条),
       中心裁切必然命中场景/角标:
         白色扫把 → 命中深灰窗帘 / 深棕木地板
         透明玻璃盒 → 命中米色场景
         红色促销角标 → 命中红色
       → 现在改成: 大图 + 背景环剔除 + 促销色剔除 + **材质先验**。
       材质先验来自**真实商品名**(如"不锈钢" → 银灰), 不是拍脑袋编的。
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f"缺少 Pillow, 无法取商品主色: {e}")

    tmp = os.path.join(tempfile.gettempdir(), f"dy_pool_{os.getpid()}.jpg")
    try:
        r = subprocess.run(["curl", "--noproxy", "*", "-m", "20", "-s", "-o", tmp,
                            _full_res_url(image_url)], capture_output=True)
        if r.returncode != 0 or not os.path.exists(tmp):
            raise RuntimeError(f"主图下载失败(curl exit {r.returncode}): {image_url}")
        if os.path.getsize(tmp) < 1000:
            raise RuntimeError(f"主图过小, 疑似下载失败: {image_url}")
        im = Image.open(tmp).convert("RGB")

        # ---- 优先级 1: 商品名里写明了颜色 → 直接采信(最准) ----
        named = detect_name_color(name)
        if named.get("hex"):
            return named["hex"]

        # ---- 优先级 2: 材质典型色(玻璃=通透 / 不锈钢=银灰 / 硅胶=浅灰…) ----
        # ⚠️ 营销合成图里像素取色会取到包装盒/地板/背景。
        #   但"玻璃/不锈钢/硅胶"这类材质的**典型色是确定的**,
        #   而材质词来自真实商品名 → 直接采信材质色, 比像素准。
        mat = detect_material(name)
        fam_hex = mat.get("family_hex")
        if fam_hex and mat.get("strong"):
            return fam_hex

        # ---- 优先级 3: 大图 + 网格投票取色(带材质先验) ----
        color = analyze_product_color(im, family_hex=fam_hex)
        if not color:
            raise RuntimeError(f"主图里找不到可用颜色: {image_url}")
        return color
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
    # ⚠️ 2026-11-09: "京东优评"栏目页长得很像榜单页(也有 alt 商品名),
    #    但**没有 item.jd.com 商品块**, 解析出来是 0 个商品。
    #    → 显式识别并报失败, 别让它在累积合并里静默变成"这个源今天没货"。
    if "优评" in title:
        print(f"   ❌ 该 URL 指向【京东优评】栏目, 不是榜单页: {url}")
        return []
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


# 自动重抓的保底阈值: 有效商品少于这个数就放弃本次重抓, 保留旧池子。
# 理由: 榜单页结构改版 / 网络抖动 会让抓取大面积失败, 此时覆盖池子等于投毒
#       —— 坏池子会让第二天的流水线在 build_feed 阶段崩掉。
MIN_POOL_SIZE = 20

# 池子商品的最长"在榜有效期"(天)。超过这个天数没在榜单页上出现过 → 剔除。
# ⚠️ 2026-11-08 加: 池子改成累积模式后只增不减, 不做老化清理的话
#    一年后池子里会堆满几百个早就掉榜的僵尸商品, 而且它们的名次早已失效。
POOL_MAX_AGE_DAYS = 60


def recolor_existing(slug: str) -> int:
    """只重算**已有池子**的 color, 不重新抓榜单页。

    ⚠️ 为什么要有这个模式 (2026-11-05):
       取色算法修好后, 池子里存的老 color 是错的(缩略图 + 中心裁切取的噪声)。
       但**不能为此重抓榜单页** ——
         - 榜单页内容会按关键词轮换, 重抓会把 44 个商品**全换掉**,
           榜单名次/评论数也跟着变, 30 天养号期的商品序列全乱;
         - AGENTS.md 明确写了: 重抓失败会把残缺池子写进磁盘, 第二天直接崩。
       → 只重算 color 这一个**派生字段**, name/sku/rank/reviews 一律不动。

    返回 0 = 成功; 1 = 失败(旧池子保持不变)。
    """
    path = os.path.join(POOL_ROOT, f"{slug}.json")
    if not os.path.exists(path):
        print(f"❌ 找不到池子 {path}", file=sys.stderr)
        return 1
    pool = read_json(path)
    products = pool.get("products") or []
    print(f"🎨 重算 {len(products)} 个商品的 color (不动 name/sku/rank/reviews) ...")

    fail = []
    for i, p in enumerate(products, 1):
        try:
            old = p.get("color")
            new = dominant_color(p.get("image", ""), p.get("name", ""))
            p["color"] = new
            mark = "  (未变)" if old == new else f"  ← 原 {old}"
            print(f"   [{i:2d}] {new}  {p.get('name', '')[:36]}{mark}")
        except Exception as e:
            fail.append((p.get("name", ""), str(e)))
            print(f"   [{i:2d}] ❌ 取色失败: {e}")

    # ⚠️ 同样的保底阈值: 取色失败面太大就**放弃本次重算**, 保留旧池子
    if len(products) - len(fail) < MIN_POOL_SIZE:
        print(f"\n❌ 有效商品仅 {len(products) - len(fail)} 个 < 阈值 {MIN_POOL_SIZE} "
              f"—— 放弃本次重算, 旧池子保持不变", file=sys.stderr)
        return 1
    write_json(path, pool)
    print(f"\n💾 已更新 {path} 的 color ({len(products)} 个商品)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="养号期垂直商品池构建器")
    ap.add_argument("--source", default=None, help="只抓指定源(名字匹配 sources.json)")
    ap.add_argument("--config", default=None, help="源配置 json (默认 pool/sources.json)")
    ap.add_argument("--recolor", action="store_true",
                    help="只重算已有池子的 color(不重抓榜单页, 不动 rank/name)")
    ap.add_argument("--replace", action="store_true",
                    help="整池替换(默认是**累积合并**: 旧商品保留, 新商品追加)")
    ap.add_argument("--slug", default=None, help="--recolor 用的池子 slug")
    args = ap.parse_args()

    if args.recolor:
        cfg_path = args.config or os.path.join(POOL_ROOT, "sources.json")
        slug = args.slug
        if not slug and os.path.exists(cfg_path):
            slug = read_json(cfg_path).get("slug") or "pool"
        return recolor_existing(slug or "pool")

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
        # ⚠️ 源之间限速 —— 连续快请求会被京东整段限流(通用错误页)
        time.sleep(_SOURCE_GAP)

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
            p["color"] = dominant_color(p["image"], p.get("name", ""))
            print(f"   [{i:2d}] {p['color']}  {p['name'][:40]}")
        except Exception as e:
            # ⚠️ 不静默兜底: 取色失败就是图文不符的种子, 必须留在日志里
            color_fail.append((p["name"], str(e)))
            p["color"] = None
            print(f"   [{i:2d}] ❌ 取色失败: {e}")

    # ⚠️ 2026-10-29 加固(为每周自动重抓做准备):
    #    取色失败的商品**绝不能进池** —— 颜色缺失 = 图文不符的种子。
    #    更关键: 自动重抓时若网络抖动导致大面积取色失败, 绝不能把残缺池子
    #    覆盖到磁盘, 那会让第二天的流水线拿到坏池子直接崩。
    #    处置: 剔除失败项; 剩余不足阈值就整体放弃本次重抓, 保留旧池子。
    good = [p for p in uniq if p.get("color")]
    dropped = len(uniq) - len(good)
    slug = cfg.get("slug") or "pool"
    os.makedirs(POOL_ROOT, exist_ok=True)
    out_path = os.path.join(POOL_ROOT, f"{slug}.json")

    if dropped:
        print(f"\n⚠️ 剔除取色失败商品 {dropped} 个 (颜色缺失=图文不符)")
    # ⚠️ 2026-11-08 修正: 阈值判定要看**合并后**的池子规模, 不是本次抓到的数量。
    #    累积模式下"本次只抓到 10 个"是**正常**的(榜单页轮换, 每次只换一部分),
    #    按本次数量判会误杀 —— 单源试跑就直接触发"放弃重抓", 累积永远做不起来。
    #    → 旧池存在时: 要求合并后总数 ≥ 阈值;
    #      新建池时: 才要求本次抓取数 ≥ 阈值(没有旧池兜底, 数据太少没法用)。
    merging = (not args.replace) and os.path.exists(out_path)
    old_count = 0
    if merging:
        try:
            old_count = len((read_json(out_path).get("products") or []))
        except Exception:
            old_count = 0
    eff_count = len(good) + old_count if merging else len(good)
    if eff_count < MIN_POOL_SIZE:
        print(f"\n❌ 有效商品仅 {eff_count} 个 < 阈值 {MIN_POOL_SIZE} —— 放弃本次重抓",
              file=sys.stderr)
        print(f"   旧池子保持不变: {out_path}", file=sys.stderr)
        return 1
    uniq = good

    slug = cfg.get("slug") or "pool"
    os.makedirs(POOL_ROOT, exist_ok=True)
    out_path = os.path.join(POOL_ROOT, f"{slug}.json")
    today = date.today().strftime("%Y-%m-%d")

    # ⚠️ 2026-11-08 改为**累积合并**(默认), 不再整池替换。
    #    原因: 30 天养号要发 150 条, 池子必须够大才能不重复。
    #    榜单页内容会按关键词轮换 → 每次重抓都能捡到一批新商品
    #    (2026-11-08 实测: 5 个源重抓一次, 46 条里有 16 条是池子没有的新品)。
    #    整池替换的话, 新品来了旧的就被冲掉, 池子永远停在 40 来个。
    #    累积合并 = 池子只增不减, 同时已存在商品的 rank/reviews 用**最新一次**
    #    抓到的值刷新(榜上名次是会变的, 用旧的反而不准)。
    if not args.replace and os.path.exists(out_path):
        old_pool = read_json(out_path)
        old_products = old_pool.get("products") or []
        by_sku = {p.get("sku"): p for p in old_products if p.get("sku")}
        added = updated = kept = 0
        aged = []
        # ⚠️ 旧池商品没有 last_seen 字段 → 用**旧池的快照日期**补上,
        #    不能当成"永远最老"。否则每次重抓都会把这批商品判成老化商品清掉,
        #    池子凭空缩水(2026-11-08 实测: 15 个老商品被误剔)。
        old_seen = old_pool.get("fetched_at") or today
        for p in old_products:
            if p.get("sku") and not p.get("last_seen"):
                p["last_seen"] = old_seen
        for p in uniq:
            pid = p.get("sku")
            old = by_sku.get(pid)
            if old:
                # 已在榜上的商品: 用新数据刷新 rank/reviews/links, 但保留旧 color
                # (color 是取色算出来的, 旧值已验证有效, 没必要重下图片)
                if old.get("color"):
                    p["color"] = old["color"]
                old.update(p)
                old["last_seen"] = today
                updated += 1
            else:
                p["last_seen"] = today
                by_sku[pid] = p
                added += 1
        merged = list(by_sku.values())
        # 旧池里这次没抓到的商品: 保留, last_seen 维持原值(它已掉榜)
        kept = len(merged) - added - updated
        print(f"\n🔄 累积合并: 新增 {added} 个 · 刷新 {updated} 个 · 保留(已掉榜) {kept} 个")
        uniq = merged
    else:
        for p in uniq:
            p["last_seen"] = today

    # ⚠️ 老化清理: 累积模式下池子只增不减, 长期掉榜的老商品会越积越多。
    #    这些商品的名次早就失效了(文案里已经不写名次), 留着只是占位。
    #    → 超过 POOL_MAX_AGE_DAYS 没在榜上出现的直接剔除。
    #    ⚠️ 旧池商品没有 last_seen 字段 → 视为最老, 会被清掉;
    #       这正是想要的(它们是 11-02 那批, 名次早就不是最新的了)。
    if merging and old_count:
        cutoff = (date.today() - timedelta(days=POOL_MAX_AGE_DAYS))
        cutoff_s = cutoff.strftime("%Y-%m-%d")
        before = len(uniq)
        alive = []
        for p in uniq:
            ls = p.get("last_seen") or ""
            if ls and ls >= cutoff_s:
                alive.append(p)
            else:
                aged.append(p.get("name", "")[:30])
        if aged:
            print(f"🧹 剔除老化商品 {len(aged)} 个 (超过 {POOL_MAX_AGE_DAYS} 天没在榜上)")
            for nm in aged[:6]:
                print(f"   · {nm}")
            if len(aged) > 6:
                print(f"   · …另外 {len(aged) - 6} 个省略")
        uniq = alive

    # 按源轮转排列(累积后也要重新排, 否则新增商品全堆在末尾)
    by_src2 = {}
    for p in uniq:
        by_src2.setdefault(p.get("source_label", ""), []).append(p)
    interleaved2 = []
    while any(by_src2.values()):
        for label in list(by_src2):
            if by_src2[label]:
                interleaved2.append(by_src2[label].pop(0))
    uniq = interleaved2

    out = {
        "vertical": vertical,
        "category": category,
        "fetched_at": today,
        "count": len(uniq),
        "color_failed": dropped,
        "products": uniq,
    }
    write_json(out_path, out)
    print(f"\n💾 写入 {out_path} ({len(uniq)} 个商品, 单垂直)")
    if color_fail:
        print(f"\n⚠️ 本次有 {len(color_fail)} 个商品取色失败, 已剔除(详见上方清单)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
