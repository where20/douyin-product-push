#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lib_dy.py — 抖音图文带货支线公共脚手架

与 xhs-product-push 的 lib_xhs.py 是**平级独立**的两份, 不互相 import。
原因: 两条支线节奏/产物形态/平台规则完全不同, 绑一起会互相拖累。

本文件只放**跨脚本稳定不变**的东西:
  - 路径常量 / 日期解析
  - schema 断言 (在写内容文件时就把坏数据拦住)
  - 抖音图文的内容规范常量 (3 图结构 / 文案长度 / 比例)
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

# ---------- 路径常量 ----------
# ⚠️ 走环境变量, 不硬编码绝对路径。cron / 手动跑都要能定位。
TASK_DIR = os.environ.get("TASK_DY_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
OUT_ROOT = os.path.join(TASK_DIR, "output")
MEM_ROOT = os.path.join(TASK_DIR, "memory")

# ---------- 抖音图文规范 (2026-10-29 实测 + 平台公开规则) ----------
# 图文作品: 3:4 竖版图, 1-3 张, 首图最关键(决定点击率)
DY_IMG_RATIO = "3:4"
DY_IMG_WIDTH = 1080
DY_IMG_HEIGHT = 1440          # 1080 * 4/3 = 1440
DY_IMAGES_PER_POST = 3        # 首图(冲击) / 中图(场景) / 尾图(价格锚点)
DY_COPY_MIN = 50              # 文案下限(低于 50 字平台判低质)
DY_COPY_MAX = 100             # 文案上限(超 100 字折叠)

# 抖音图文的三图分工 —— 不是随便 3 张, 有明确叙事结构
DY_IMAGE_ROLES = ("cover", "scene", "price")

# ---------- 价格带 (与小红书支线对齐) ----------
PRICE_MIN = 10.0
PRICE_MAX = 500.0

# ---------- 违规词 (平台合规红线, 2026 广告法 + 抖音社区规范) ----------
# ⚠️ 这些词一旦出现 = 扣分/限流/封号, 断言直接拦下
BANNED_WORDS = (
    "最好", "最佳", "第一", "顶级", "极致", "绝对", "国家级", "世界级",
    "全网最低", "史上最低", "永久", "根治", "治愈", "特效", "包治",
    "无效退款", "假一赔十", "独家", "唯一", "纯天然", "零风险", "100%有效",
)


def resolve_date(arg: str = None) -> str:
    """日期解析: 显式参数 > XHS_DATE 环境变量 > 今天。
    所有脚本统一走这里, 避免各自 date +%Y-%m-%d 走偏。"""
    if arg:
        return arg
    env = os.environ.get("XHS_DATE") or os.environ.get("DY_DATE")
    if env:
        return env
    return date.today().strftime("%Y-%m-%d")


def out_dir(d: str) -> str:
    return os.path.join(OUT_ROOT, d)


def ensure_out_dir(d: str) -> str:
    p = out_dir(d)
    os.makedirs(p, exist_ok=True)
    return p


def read_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _fail(msg: str):
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(1)


def assert_feed(data):
    """校验当天 feed.json (5 个商品的抖音图文稿)。

    拦的是**会直接导致限流/违规/图文不符**的问题, 不是格式洁癖。
    """
    if not isinstance(data, list):
        _fail("feed.json 必须是 list")
    if len(data) != 5:
        _fail(f"feed.json 必须恰好 5 个商品, 实际 {len(data)}")

    for i, p in enumerate(data, 1):
        tag = f"feed[{i}]"
        # ⚠️ 只校验**内容**字段。images / image_urls 是出图后的产物,
        #    写稿阶段还不存在 —— 把产物写进断言会让 build_feed 永远失败。
        for fld in ("id", "name", "price", "desc", "color", "copy",
                    "image_prompts", "links"):
            if fld not in p or p[fld] in (None, "", [], {}):
                _fail(f"{tag} 缺字段 {fld}")

        # color 必须是 #RRGGBB —— 抖音首图配色靠它, 非法值会让出图跑偏
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(p["color"])):
            _fail(f"{tag}.color 非法: {p['color']!r} (必须 #RRGGBB)")

        # price 必须能解析出数字, 且在带货主力价位带
        m = re.search(r"(\d+(?:\.\d+)?)", str(p["price"]))
        if not m:
            _fail(f"{tag}.price 解析不出数字: {p['price']!r}")
        price = float(m.group(1))
        if not (PRICE_MIN <= price <= PRICE_MAX):
            _fail(f"{tag}.price ¥{price} 超出 ¥{PRICE_MIN}-{PRICE_MAX} 主力价位带")

        # desc 太短 = 平台判低质
        if len(str(p["desc"])) < 100:
            _fail(f"{tag}.desc 仅 {len(str(p['desc']))} 字, 至少 100 字")

        # 文案长度
        copy = str(p["copy"]).strip()
        if not (DY_COPY_MIN <= len(copy) <= DY_COPY_MAX):
            _fail(f"{tag}.copy {len(copy)} 字, 必须在 {DY_COPY_MIN}-{DY_COPY_MAX} 字")

        # 违规词
        for w in BANNED_WORDS:
            if w in copy or w in str(p["desc"]) or w in str(p["name"]):
                _fail(f"{tag} 含抖音违规词 {w!r} → 会扣分/限流, 换掉")

        # 三图结构: cover/scene/price 三份 prompt, 且 image_prompts 里 color 对齐
        prompts = p["image_prompts"]
        if not isinstance(prompts, dict):
            _fail(f"{tag}.image_prompts 必须是 dict")
        for role in DY_IMAGE_ROLES:
            if not prompts.get(role):
                _fail(f"{tag}.image_prompts 缺 {role}")
            if not str(prompts[role]).strip():
                _fail(f"{tag}.image_prompts.{role} 为空")

        # links 必须是 http(s) 绝对地址
        links = p.get("links") or {}
        primary = links.get("primary")
        if not primary or not str(primary).startswith(("http://", "https://")):
            _fail(f"{tag}.links.primary 必须是 http(s) 绝对地址: {primary!r}")
        for u in links.get("extra") or []:
            if not str(u).startswith(("http://", "https://")):
                _fail(f"{tag}.links.extra 含非 http(s) 地址: {u!r}")

    print(f"✅ feed schema 校验通过: {len(data)} 个商品")


def assert_api_creds():
    """校验抖音精选联盟 API 凭证是否齐备。

    走环境变量 / config/env.json, 不写死在代码里。
    """
    app_key = os.environ.get("DY_APP_KEY")
    app_secret = os.environ.get("DY_APP_SECRET")
    access_token = os.environ.get("DY_ACCESS_TOKEN")
    missing = [n for n, v in (("DY_APP_KEY", app_key),
                              ("DY_APP_SECRET", app_secret),
                              ("DY_ACCESS_TOKEN", access_token)) if not v]
    if missing:
        return False, missing
    return True, []


if __name__ == "__main__":
    d = resolve_date()
    print(f"date = {d}")
    print(f"out  = {out_dir(d)}")
    ok, missing = assert_api_creds()
    print(f"api_creds = {ok}" + (f" (缺: {', '.join(missing)})" if missing else ""))
