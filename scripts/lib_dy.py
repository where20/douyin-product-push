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
POOL_ROOT = os.path.join(TASK_DIR, "pool")

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

# ---------- 账号阶段 (2026-10-29 调研固化) ----------
# ⚠️ **新号前 20 条别挂小黄车** —— 系统默认带货内容营销属性强,
#    会给自然流量打折扣; 前 7 天挂购物车/导流/第三方联系方式 = 违规降权。
#    所以"养号"和"带货"是**两个阶段**, 内容形态完全不同。
#
#   warmup   (0-1000 粉): 普通图文, 不挂车, 不带价格锚点, 走垂直干货/情绪路线
#   monetize(1000+ 粉): 带货图文, 挂小黄车, 价格锚点
STAGE_WARMUP = "warmup"
STAGE_MONETIZE = "monetize"
DY_STAGES = (STAGE_WARMUP, STAGE_MONETIZE)

# 阶段说明(写进发布清单, 防止手滑在养号期挂车)
STAGE_INFO = {
    STAGE_WARMUP: {
        "label": "养号期",
        "rule": "不挂小黄车 · 不带价格 · 不写第三方联系方式",
        "why": "新号前 20 条挂车会被判定为营销号, 自然流量打折; 前 7 天导流直接降权",
        "can_cart": False,
    },
    STAGE_MONETIZE: {
        "label": "带货期",
        "rule": "挂小黄车 · 带价格锚点 · 引导橱窗下单",
        "why": "图文挂车需 1000 有效粉丝; 0 粉只能开基础橱窗(每周限挂 1 款)",
        "can_cart": True,
    },
}

# 阶段默认阈值(有效粉丝) —— 用来自动判断当前该走哪条路
STAGE_FANS_THRESHOLD = {STAGE_WARMUP: 0, STAGE_MONETIZE: 1000}

# ---------- 价格带 (与小红书支线对齐) ----------
PRICE_MIN = 10.0
PRICE_MAX = 500.0

# ---------- 违规词 (平台合规红线, 2026 广告法 + 抖音社区规范) ----------
# ⚠️ 这些词一旦出现 = 扣分/限流/封号, 断言直接拦下
BANNED_WORDS = (
    "最好", "最佳", "顶级", "极致", "国家级", "世界级",
    "全网最低", "史上最低", "永久", "包治",
    "无效退款", "假一赔十", "独家", "唯一", "纯天然", "零风险", "100%有效",
)

# ⚠️ **上面某些词有正常语义用法**, 裸匹配会误伤。
#    例: "第一步就做错了" 里的"第一"是正常口语, 但"行业第一"是违规广告语。
#    2026-10-29 实测踩到: warmup 文案模板里的"第一步"被误判 → 断言直接拦下。
#    → 对这类词用"上下文模式"判定, 只有命中广告语境才算违规。
BANNED_PATTERNS = (
    # "第一" 只有接品牌/名次/销量类词时才算违规
    (r"第一\s*(品牌|销量|品质|名次|名牌|档次|高端|大品牌|官方)", "第一"),
    (r"(行业|全国|全球|市场|销量|品质|技术|科技)第一", "第一"),
    # "绝对" 只有接正面承诺词时才算违规
    (r"绝对\s*(安全|可靠|有效|正品|放心|不会|保证|值得|划算|好用)", "绝对"),
    # 功效类词: 指医疗/功效时违规
    (r"根治|治愈", "根治/治愈"),
    (r"特效", "特效"),
)


def find_banned(text: str):
    """返回命中的违规词列表。

    ⚠️ 裸词直接拦(那些没有正常用法的);
       上下文词走正则模式判定(避免"第一步"这类正常口语被误伤)。
    """
    hits = []
    t = str(text or "")
    for w in BANNED_WORDS:
        if w in t:
            hits.append(w)
    for pat, label in BANNED_PATTERNS:
        if re.search(pat, t):
            hits.append(label)
    return sorted(set(hits))


# ⚠️ **养号期专属禁写清单** —— 带货期的"营销感"在养号期是毒药。
#    新号前 20 条只要出现价格/下单信号, 系统就判营销号, 自然流量直接打折。
#    这些词在带货期是正常的, 但在 warmup 阶段一个字都不能出现。
WARMUP_FORBIDDEN = (
    "¥", "￥", "到手", "下单", "加购", "橱窗", "小黄车", "购物车",
    "包邮", "优惠券", "券后", "秒杀", "库存", "抢", "链接", "下单",
)


# ============================================================
# 商品语义解析 —— 从**真实商品名**抠 材质 / 形态 / 颜色
# ============================================================
# ⚠️ 2026-11-05 实测踩到(根因; 11-02 / 11-04 已两次记录但未修):
#   原来 product_en / color 都从**榜单页类目**推。结果商品形态全错:
#     "佳帮手EVA魔术扫把刮水器地刮" 被当成 "broom and dustpan set"
#     → 出图画成扫把+簸箕, 实物是 EVA 地刮 → 图文不符 = 假带货(硬规则 #4)。
#   榜单页一个类目下混着完全不同的商品: 同一个"清洁工具簸箕"榜里有
#   扫把簸箕套装, 也有 EVA 地刮 / 硅胶刮水器 / 平板拖把。
#   → 形态/材质/颜色全部从**真实 JD 商品名**里抠, 不按类目猜。

# (关键词, 英文材质修饰, 典型色hex, 英文色名, 色是否权威)
# ⚠️ family_hex 不是拍脑袋编的颜色, 是**该材质的典型外观色**:
#    玻璃=通透 / 不锈钢=银灰 / 硅胶=浅灰 / EVA=浅灰 / 塑料=白 / 高粱=草黄 / 原木=棕木。
#    材质词来自**真实 JD 商品名**, 属于真实数据。
# ⚠️ strong=True 表示"这个材质的典型色是确定的", 取色时**直接采信材质色**:
#    营销合成图里像素取色只会取到包装盒/地板/背景墙, 而玻璃必通透、不锈钢必银灰。
#    strong=False 表示"颜色不固定"(布艺箱有米色/灰/黑/藏青多种) → 交给像素投票。
_MATERIAL_RULES = (
    ("高硼硅", "borosilicate glass", "#C6D5D8", "clear transparent", True),
    ("钢化玻璃", "tempered glass", "#C6D5D8", "clear transparent", True),
    ("耐热玻璃", "heat-resistant glass", "#C6D5D8", "clear transparent", True),
    ("玻璃", "glass", "#C6D5D8", "clear transparent", True),
    ("不锈钢", "stainless steel", "#B2B4B6", "silver metallic", True),
    ("铝合金", "aluminium", "#B8BABC", "silver metallic", True),
    ("硅胶", "silicone", "#D4D6D8", "soft light grey", True),
    ("eva", "EVA foam", "#D6D8DA", "soft light grey", True),
    ("塑料", "plastic", "#DEDEDC", "clean white", True),
    ("高粱", "sorghum", "#B5894B", "natural straw tan", True),
    ("原木", "natural wood", "#A87C4E", "warm natural wood brown", True),
    ("实木", "solid wood", "#A87C4E", "warm natural wood brown", True),
    ("竹制", "bamboo", "#C7A878", "light natural bamboo tan", True),
    ("牛津布", "oxford fabric", None, None, False),
    ("无纺布", "non-woven fabric", None, None, False),
    ("布艺", "fabric", None, None, False),
)

# (关键词, hex, 英文色名) —— 商品名里写明颜色时**直接采信**, 比任何取色算法都准
# 顺序敏感: "淡雅米" 必须排在 "米" 前面, "深灰色" 必须排在 "灰色" 前面
_COLOR_RULES = (
    ("淡雅米", "#E8DFD0", "cream beige"),
    ("米白", "#F0EAE0", "off-white"),
    ("浅米", "#EFE6D8", "light beige"),
    ("米黄", "#E8DCC0", "beige"),
    ("米色", "#E6DCC8", "beige"),
    ("浅灰", "#C8C8C8", "light grey"),
    ("深灰", "#55565A", "dark grey"),
    ("黑灰", "#3A3A3C", "dark charcoal grey"),
    ("银白", "#D8DCE0", "silver white"),
    ("银色", "#C0C4C8", "silver"),
    ("透明", "#DCE6E8", "clear transparent"),
    ("黄色", "#E8C020", "yellow"),
    ("红色", "#C02828", "red"),
    ("蓝色", "#2A4A7A", "blue"),
    ("绿色", "#2E7A46", "green"),
    ("粉色", "#E0A0B0", "pink"),
    ("紫色", "#6A4A8A", "purple"),
    ("棕色", "#7A5230", "brown"),
    ("咖色", "#5A3E2A", "coffee brown"),
    ("黑色", "#1E1E20", "black"),
    ("白色", "#F2F2F0", "white"),
    ("灰色", "#8A8A8A", "grey"),
)


def detect_material(name: str) -> dict:
    """从商品名解析材质 → {modifier, family_hex, family_en, strong, keyword}。

    family_hex = 该材质的典型色, 用作**取色先验**(不是硬编造):
    strong=True 时直接采信该色, 否则只作为像素投票的参考。
    """
    s = str(name or "").lower()
    for kw, mod, fam_hex, fam_en, strong in _MATERIAL_RULES:
        if kw.lower() in s:
            return {"modifier": mod, "family_hex": fam_hex, "family_en": fam_en,
                    "strong": strong, "keyword": kw}
    return {"modifier": "", "family_hex": None, "family_en": None,
            "strong": False, "keyword": ""}


def detect_name_color(name: str) -> dict:
    """商品名里写明了颜色 → 直接采信(最高置信度)。

    ⚠️ 只认**完整颜色词**, 不认单字"米/白/黑" —— 商品名里
    "厘米"/"明白"之类会误伤。
    """
    s = str(name or "")
    for kw, hexv, en in _COLOR_RULES:
        if kw in s:
            return {"hex": hexv, "en": en, "keyword": kw}
    return {"hex": None, "en": None, "keyword": ""}


def derive_product_en(name: str, fallback: str = "") -> str:
    """从商品名解析**英文品类词**(给 mmx prompt 用, 不含品牌名)。

    ⚠️ 品牌名绝不能进 prompt —— 模型会把品牌当文字画到商品包装上
       (2026-10-29 实测: prompt 写"八海鸟收纳箱" → 包装上出现乱码 "Hahdniao")。
    """
    s = str(name or "")
    has = lambda *ws: any(w in s for w in ws)

    # ---- 清洁工具族 ----
    if has("平板拖把") or (has("拖把") and has("拖把桶", "托把桶")):
        base = "flat floor mop with matching bucket"
    elif has("刮水器", "地刮", "刮地", "玻璃刮", "扫水神器"):
        base = "floor squeegee"
    elif has("拖把"):
        base = "flat floor mop"
    elif has("簸箕", "撮箕", "畚箕", "垃圾斗", "垃圾铲"):
        base = ("broom and dustpan set" if has("扫把", "扫帚", "笤帚")
                else "dustpan and floor grabber set")
    elif has("扫把", "扫帚", "笤帚"):
        base = "broom"
    # ---- 收纳箱族 ----
    elif has("车载", "后备箱"):
        base = "car trunk organizer box"
    elif has("露营", "户外", "出游", "旅行", "暑假"):
        base = "foldable outdoor travel storage box"
    elif has("玩具"):
        base = "toy storage box"
    elif has("书本", "书籍"):
        base = "moving storage box"
    elif has("保鲜盒", "饭盒", "便当盒", "保鲜碗"):
        base = "food storage container set"
    elif has("收纳箱", "储物箱", "整理箱", "收纳盒", "百纳箱", "整理筐", "储物筐"):
        base = "storage box"
    else:
        return fallback or "home storage product"

    # ---- 形态修饰 ----
    mods = []
    if has("翻盖"):
        mods.append("flip-lid")
    if has("带轮", "滑轮"):
        mods.append("on wheels")
    if has("带盖"):
        mods.append("with lid")
    if has("分格", "分隔", "隔断"):
        mods.append("with internal dividers")
    if has("侧开门"):
        mods.append("with side opening")
    if has("铝槽", "可伸缩"):
        mods.append("with aluminium channel")
    if has("大容量", "特大号"):
        mods.append("large capacity")

    mat = detect_material(s)["modifier"]
    parts = ([mat] if mat else []) + mods[:2] + [base]
    return " ".join(parts)


def _rgb_of(hex_color: str):
    h = str(hex_color or "").lstrip("#")
    if len(h) != 6:
        return None
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return None


def _hue_sat(rgb):
    r, g, b = rgb
    mx, mn = max(rgb), min(rgb)
    sat = (mx - mn) / mx if mx else 0.0
    if mx == mn:
        return 0.0, sat
    if mx == r:
        h = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        h = (b - r) / (mx - mn) + 2
    else:
        h = (r - g) / (mx - mn) + 4
    return h * 60, sat


def _is_promo_color(rgb) -> bool:
    """京东促销色(价格角标/促销条): 纯红 / 高饱和橙。"""
    h, s = _hue_sat(rgb)
    if (h < 20 or h >= 340) and s > 0.45:
        return True
    if 20 <= h < 45 and s > 0.75:
        return True
    return False


def analyze_product_color(im, family_hex: str = None, tol: int = 110,
                          grid: int = 3):
    """从**商品图**里取商品主色(纯 PIL, 不依赖 numpy)。

    2026-11-05 重写。老实现是"中心 60% 裁切 + 取像素最多的合格色",
    在京东**营销合成图**上会取到: 促销角标红 / 地板棕 / 背景米色,
    5 组实测 4 组图文不符。

    新流程 —— **网格投票**:
      1) 把图切成 grid×grid 小格, 每格取众数色(排除促销红/橙、近白背景)
      2) 各格的众数色再投票, 越靠中心的格权重越高
         (商品主体在中间, 促销条/价格角标在边缘)
      3) 有材质先验(family_hex) → 在投票结果里挑离先验最近的

    为什么不用"整图众数色": 整图众数色会被包装盒/地板/背景墙主导。
    为什么不用"边缘色排除": 黑色商品放在深色车厢里, 边缘环=黑,
    排除边缘色会把商品本体也排除掉。

    返回 #RRGGBB 或 None。
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(f"缺少 Pillow, 无法取商品主色: {e}")

    im = im.convert("RGB")
    w, h = im.size
    tw, th = max(1, w // grid), max(1, h // grid)
    votes = {}
    for ty in range(grid):
        for tx in range(grid):
            tile = im.crop((tx * tw, ty * th, (tx + 1) * tw, (ty + 1) * th))
            c = _mode_color(tile)
            if c is None:
                continue
            ccx, ccy = (tx + 0.5) / grid, (ty + 0.5) / grid
            dist = ((ccx - 0.5) ** 2 + (ccy - 0.5) ** 2) ** 0.5
            wgt = 1.0 + 1.2 * (1 - min(1.0, dist / 0.7))
            votes[c] = votes.get(c, 0) + wgt
    if not votes:
        fam = _rgb_of(family_hex)
        return "#%02X%02X%02X" % fam if fam else None

    fam = _rgb_of(family_hex)
    if fam:
        def d(c):
            return sum((a - b) ** 2 for a, b in zip(c, fam)) ** 0.5
        near = [(d(c), c) for c in votes if d(c) <= tol]
        if near:
            return "#%02X%02X%02X" % min(near)[1]
        return "#%02X%02X%02X" % fam

    return "#%02X%02X%02X" % max(votes.items(), key=lambda kv: kv[1])[0]


def _mode_color(im):
    """一块图区域里的众数色(排除促销色与近白背景)。"""
    cnt = {}
    for p in im.getdata():
        mx, mn = max(p), min(p)
        if mx > 235 and mn > 225:      # 近白 = 背景
            continue
        if mx < 28:                   # 近黑 = 阴影
            continue
        if _is_promo_color(p):         # 京东促销角标
            continue
        cnt[p] = cnt.get(p, 0) + 1
    if not cnt:
        return None
    return max(cnt.items(), key=lambda kv: kv[1])[0]


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


def _main_cat(category: str) -> str:
    """类目取第一段("家居日用 · 收纳清洁" → "家居日用")。"""
    return re.split(r"[·|｜]", str(category or ""))[0].strip()


def assert_feed(data):
    """校验当天 feed.json (5 个商品的抖音图文稿)。

    拦的是**会直接导致限流/违规/图文不符**的问题, 不是格式洁癖。

    ⚠️ 断言**阶段化**(2026-10-29 加):
       养号期(warmup)的数据源是垂直池 —— 真实商品名/榜单名次/评论数/主图,
       但**没有价格也没有 desc**(京东榜单页抓不到这两样)。
       所以养号期: 价格/desc 可空, 但**必须单垂直 + 文案零营销信号**。
       带货期(monetize): 价格必须是真实数字且在主力价位带, desc ≥100 字。
    """
    if not isinstance(data, list):
        _fail("feed.json 必须是 list")
    if len(data) != 5:
        _fail(f"feed.json 必须恰好 5 个商品, 实际 {len(data)}")

    stage = (data[0].get("stage") if data else None) or STAGE_WARMUP
    if stage not in DY_STAGES:
        _fail(f"未知 stage: {stage!r}")
    warmup = stage == STAGE_WARMUP

    for i, p in enumerate(data, 1):
        tag = f"feed[{i}]"
        # ⚠️ 只校验**内容**字段。images / image_urls 是出图后的产物,
        #    写稿阶段还不存在 —— 把产物写进断言会让 build_feed 永远失败。
        for fld in ("id", "name", "color", "copy", "image_prompts", "links"):
            if fld not in p or p[fld] in (None, "", [], {}):
                _fail(f"{tag} 缺字段 {fld}")

        # color 必须是 #RRGGBB —— 抖音首图配色靠它, 非法值会让出图跑偏
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(p["color"])):
            _fail(f"{tag}.color 非法: {p['color']!r} (必须 #RRGGBB)")

        # ---- 价格 ----
        # 带货期: 必须能解析出数字, 且在带货主力价位带
        # 养号期: 允许空(价格压根不展示), 但**不允许**出现解析不出来的假价格
        raw_price = p.get("price")
        if not warmup:
            if not raw_price:
                _fail(f"{tag}.price 为空 —— 带货期价格必须来自真实接口")
            m = re.search(r"(\d+(?:\.\d+)?)", str(raw_price))
            if not m:
                _fail(f"{tag}.price 解析不出数字: {raw_price!r}")
            price = float(m.group(1))
            if not (PRICE_MIN <= price <= PRICE_MAX):
                _fail(f"{tag}.price ¥{price} 超出 ¥{PRICE_MIN}-{PRICE_MAX} 主力价位带")
        elif raw_price:
            if not re.search(r"(\d+(?:\.\d+)?)", str(raw_price)):
                _fail(f"{tag}.price 解析不出数字: {raw_price!r}")

        # ---- desc ----
        # 带货期: 至少 100 字(平台判低质)
        # 养号期: 池子商品没有 desc, 用榜单事实代替, 允许空
        if not warmup and len(str(p.get("desc", ""))) < 100:
            _fail(f"{tag}.desc 仅 {len(str(p.get('desc', '')))} 字, 至少 100 字")

        # 文案长度
        copy = str(p["copy"]).strip()
        if not (DY_COPY_MIN <= len(copy) <= DY_COPY_MAX):
            _fail(f"{tag}.copy {len(copy)} 字, 必须在 {DY_COPY_MIN}-{DY_COPY_MAX} 字")

        # 违规词
        for fld in ("copy", "desc", "name"):
            hits = find_banned(p.get(fld, ""))
            if hits:
                _fail(f"{tag}.{fld} 含抖音违规词 {hits} → 会扣分/限流, 换掉")

        # ---- 养号期专属: 零营销信号 ----
        if warmup:
            bad = [w for w in WARMUP_FORBIDDEN if w in copy]
            if bad:
                _fail(f"{tag}.copy 含养号期禁写营销信号 {bad} → "
                      f"新号带价格/下单词会被判营销号, 自然流量打折")

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

    # ---- 养号期专属: 单垂直 ----
    # ⚠️ 抖音靠内容判定账号标签, 类目一散标签就散, 涨粉就慢, 图文挂车永远不解锁。
    if warmup:
        cats = {_main_cat(p.get("category", "")) for p in data}
        if len(cats) > 1:
            _fail(f"养号期必须单垂直, 实际跨了 {len(cats)} 个类目: {sorted(cats)}")

    print(f"✅ feed schema 校验通过: {len(data)} 个商品 "
          f"(stage={stage}"
          f"{', 单垂直' if warmup else ''})")


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
