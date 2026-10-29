#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_feed.py — 选品数据 → 抖音图文稿 feed.json

用法:
    python3 scripts/build_feed.py <date> [--input 选品json]

输入 (默认 output/{date}/picked.json, 由 dy_api.py 产出):
    [{"id","title"/"name","price","commission_rate","sales","good_rate",
      "image","category","desc","links"}]

输出 output/{date}/feed.json:
    5 个商品, 每个含:
      - copy        50-100 字图文文案 (痛点→方案→价格刺激)
      - image_prompts {cover, scene, price}  3 图各自的 mmx prompt
      - links       真实商品页 URL
      - color       #RRGGBB 商品主色

⚠️ 文案里的**每一个事实**都必须来自输入数据, 不编造:
    价格/佣金/销量/好评率 ← 选品接口
    描述      ← 选品接口的 desc 或人工补充的真实参数
    禁止编造"库存只剩N件"/"已有X人回购"这类平台查不到的数字。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (DY_COPY_MAX, DY_COPY_MIN, assert_feed, ensure_out_dir,  # noqa: E402
                     out_dir, read_json, resolve_date, write_json)

# 抖音图文文案公式: 痛点 + 解决方案 + 价格刺激
# ⚠️ 价格刺激用**真实价格对比**, 不编造"原价999现价40.9"


def build_copy(p: dict) -> str:
    """生成 50-100 字图文文案。全部事实来自输入。"""
    name = str(p.get("name") or p.get("title") or "").strip()
    # 名字太长会挤掉文案空间, 截到品牌+品类
    short = re.sub(r'\s+', ' ', name)[:22]
    price = str(p.get("price", "")).strip()
    # ⚠️ price 可能是 "¥40.9(24包整箱·京东自营)", 文案里只放**数字**,
    #    规格信息留给尾图大字区 —— 文案里塞括号规格会被折叠且很难读。
    pm = re.search(r"(\d+(?:\.\d+)?)", price)
    price_num = f"¥{pm.group(1)}" if pm else price
    cat = str(p.get("category", "")).strip()
    desc = str(p.get("desc", "")).strip()

    # 从真实描述里提炼第一个卖点
    hook = desc[:38] if desc else f"{cat}刚需好物"

    parts = [
        f"{cat}党看过来，{short}。",
        hook,
        f"现在到手{price_num}。",
    ]
    copy = "".join(parts)

    # 超长就截到 100 字内的完整句
    if len(copy) > DY_COPY_MAX:
        copy = copy[:DY_COPY_MAX - 1]
        cut = copy.rfind("。")
        if cut > DY_COPY_MIN:
            copy = copy[:cut + 1]
        else:
            # 没有句号就补一个, 别半句结尾
            copy = copy.rstrip("，,、 ") + "。"
    return copy


def build_prompts(p: dict) -> dict:
    """生成三图 prompt。

    ⚠️ 三个坑:
    1. **颜色必须与 color 字段一致**(10-26 踩坑: 卡片写黑、图是白 = 图文不符 = 假带货)
    2. cover 要"强视觉冲击"(产品特写/堆叠/大占幅), scene 要"使用场景"
    3. 不写任何文字进画面(AI 写中文必乱码), 文字全部后期 PIL 叠加
    """
    name = str(p.get("name") or p.get("title") or "").strip()
    color = str(p.get("color", "#F2F2F2")).strip()
    cat = str(p.get("category", "")).strip()

    color_en = {
        "#F0E6DA": "warm cream beige",
        "#F2F2F0": "clean white",
        "#1F1F1F": "matte black",
        "#2B2B2E": "matte black",
        "#C9A227": "golden yellow",
    }.get(color.upper(), color)

    common = (f"of {name}, product color {color_en}, "
              f"e-commerce minimalist photography, soft natural daylight, "
              f"clean background, no text, no logo, no watermark, no price tag")

    cover = (f"Striking hero product close-up {common}, "
             f"large product filling the frame, dynamic diagonal composition, "
             f"high contrast, eye-catching first impression, "
             f"vertical 3:4 format, premium commercial styling")

    scene = (f"Lifestyle usage scene {common}, "
             f"product placed in a realistic everyday {cat} situation, "
             f"soft focus background, natural props, warm authentic mood, "
             f"vertical 3:4 format")

    # price 图由 PIL 生成, 这里给一个兜底 prompt(万一将来改成 AI 生成)
    price = (f"Clean minimal product backdrop {common}, "
             f"empty lower area for text overlay, soft studio lighting, "
             f"vertical 3:4 format")

    return {"cover": cover, "scene": scene, "price": price}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--input", default=None, help="选品 json 路径")
    args = ap.parse_args()
    d = resolve_date(args.date)
    od = ensure_out_dir(d)

    src = args.input or os.path.join(od, "picked.json")
    if not os.path.exists(src):
        print(f"❌ 找不到选品文件 {src}", file=sys.stderr)
        print("   先跑: python3 scripts/dy_api.py --mock > output/{date}/picked.json",
              file=sys.stderr)
        return 2
    picked = read_json(src)
    if isinstance(picked, dict):
        picked = picked.get("products") or picked.get("data", {}).get("products") or []

    feed = []
    for i, p in enumerate(picked[:5], 1):
        name = str(p.get("name") or p.get("title") or "").strip()
        # ⚠️ price 归一化: 源数据可能已是 "¥40.9(...)" 也可能只是数字 40.9。
        #    统一成 "¥{数字}(规格)" 格式, 避免出现 "¥¥40.9"。
        raw_price = p.get("price")
        pm = re.search(r"(\d+(?:\.\d+)?)", str(raw_price or ""))
        if not pm:
            print(f"❌ 商品 {p.get('id', i)} 的 price 解析不出数字: {raw_price!r}",
                  file=sys.stderr)
            return 1
        price_num = float(pm.group(1))
        spec = re.sub(r"^.*?\d+(?:\.\d+)?", "", str(raw_price or "")).strip("()（）·,， ")
        if not spec:
            spec = str(p.get("price_note") or p.get("category") or "")
        price_display = f"¥{price_num:g}" + (f"({spec})" if spec else "")

        item = {
            "id": p.get("id", i),
            "name": name,
            "price": price_display,
            "price_num": price_num,
            "price_note": spec,
            "category": p.get("category", ""),
            "desc": str(p.get("desc", "")).strip(),
            "color": p.get("color", "#F2F2F2"),
            "commission_rate": p.get("commission_rate"),
            "sales": p.get("sales"),
            "good_rate": p.get("good_rate"),
            "image": p.get("image", ""),
            "links": p.get("links") or {},
        }
        item["copy"] = p.get("copy") or build_copy(item)
        item["image_prompts"] = p.get("image_prompts") or build_prompts(item)
        feed.append(item)

    out = os.path.join(od, "feed.json")
    write_json(out, feed)
    print(f"📝 写入 {out} ({len(feed)} 个商品)")

    for item in feed:
        n = len(item["copy"])
        print(f"   [{item['id']}] copy {n} 字 | {item['copy'][:40]}...")

    # schema 断言: 失败直接 raise, 不让坏数据流到出图
    try:
        assert_feed(feed)
    except SystemExit as e:
        print(f"❌ feed 校验失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
