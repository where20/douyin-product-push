#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_feed.py — 选品数据 → 抖音图文稿 feed.json

用法:
    python3 scripts/build_feed.py <date> --stage warmup --pool home_storage
    python3 scripts/build_feed.py <date> --stage monetize

输入 (二选一):
    --pool <slug>   垂直商品池 pool/{slug}.json (养号期用)
                    → 真实 name/sku/榜单名次/评论数/主图, **无价格无 desc**
    picked.json     dy_api.py 产出的选品 (带货期用)
                    → 真实 name/price/desc/commission/links

输出 output/{date}/feed.json:
    5 个商品, 每个含:
      - copy        50-100 字图文文案
      - image_prompts {cover, scene, price}  3 图各自的 mmx prompt
      - links       真实商品页 URL
      - color       #RRGGBB 商品主色

⚠️ 文案里的**每一个事实**都必须来自输入数据, 不编造:
    价格/佣金/销量/好评率 ← 选品接口
    榜单名次/评论数        ← 京东榜单页
    描述      ← 选品接口的 desc 或人工补充的真实参数
    禁止编造"库存只剩N件"/"已有X人回购"这类平台查不到的数字。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (DY_COPY_MAX, DY_COPY_MIN, DY_STAGES, POOL_ROOT,  # noqa: E402
                     STAGE_INFO, STAGE_MONETIZE, STAGE_WARMUP,
                     assert_feed, ensure_out_dir, out_dir, read_json,
                     resolve_date, write_json)


# ============================================================
# 语义切分 —— 2026-10-29 重写, 修掉"46 字"那个真 bug
# ============================================================
# ⚠️ 旧实现 _first_sentence() 有两个致命问题:
#    1) 处理**剩余文本**时, 分句正则会命中开头残留的句号 → 直接返回空串。
#       (desc[:hook] 切完剩下的是 ".洁柔粉Face系列…" → hook2 变空
#        → 文案只剩 46 字, 低于 50 字下限, 断言直接拦下)
#    2) 只按**第一个**逗号切, 长句被砍成半句, 拼出来是断裂病句。
# → 改成"先切句 → 再切子句 → 再按预算打包成组", 每组都是完整语义单元。

_SENT_END = "。！？!?"
_CLAUSE_SEP = "，,;；"


def _units(text: str) -> list:
    """把描述切成语义单元列表(不带标点)。

    切法: 句末标点 → 句; 句内逗号/分号 → 子句。
    ⚠️ 只按逗号切, **不按顿号切** —— "整箱24包、单包110抽的三层结构"
       拆开读着别扭, 顿号是并列短语内部的连接。
    """
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = s.strip("，,。.、；;！!？? ")
    if not s:
        return []
    units = []
    for sent in re.split(f"[{re.escape(_SENT_END)}]+", s):
        sent = sent.strip("，,。.、；; ")
        if not sent:
            continue
        for cl in re.split(f"[{re.escape(_CLAUSE_SEP)}]+", sent):
            cl = cl.strip("，,。.、；; ")
            if cl:
                units.append(cl)
    return units


def _pack(units: list, limit: int) -> list:
    """把语义单元打包成若干组, 每组不超过 limit 字, 组内用逗号连接。

    ⚠️ 组内用逗号而不是句号 —— 逗号是原文的句内层级, 句号是原文的句边界。
       混用会把"洁柔粉Face系列精选原生木浆"这种半句升格成独立句, 读着突兀。
    """
    groups = []
    cur = ""
    for u in units:
        if not cur:
            cur = u if len(u) <= limit else u[:limit]
            if len(cur) >= limit:
                groups.append(cur)
                cur = ""
        elif len(cur) + 1 + len(u) <= limit:
            cur += "，" + u
        else:
            groups.append(cur)
            cur = u if len(u) <= limit else u[:limit]
            if len(cur) >= limit:
                groups.append(cur)
                cur = ""
    if cur:
        groups.append(cur)
    return groups


def _brand_prefix(name: str, limit: int = 18) -> str:
    """取商品名的**品牌+品类**部分, 不截半截。

    ⚠️ 裸截断会把 "洁柔 C&S 粉Face 抽纸" 截成 "洁柔 C&" —— 品牌名残缺,
    读者对不上实物, 反而降低信任。
    → 按分隔符(空格/·/-)切, 取到 limit 为止的**完整词**。
    """
    s = re.sub(r"\s+", " ", name).strip()
    if len(s) <= limit:
        return s
    parts = re.split(r"(\s+)", s)
    out = ""
    for part in parts:
        if len(out) + len(part) > limit:
            break
        out += part
    out = out.strip(" ·-—、,，")
    return out or s[:limit]


def _split_brand(name: str, keywords: list) -> tuple:
    """从中文商品名里切出 (品牌, 品类词)。

    ⚠️ 池子里的商品名**没有空格分隔**(如"百草园收纳箱衣服裤子收纳盒筐布艺..."),
       按空格切会整段落下, 按字符硬切会切出"百草园收纳箱衣服裤子收纳盒筐布艺内衣柜"
       这种半截词 —— 读者对不上实物。
    → 用类目关键词定位: 关键词第一次出现的位置之前 = 品牌。

    ⚠️ 取的是**最早命中**的那个关键词, 不是关键词表里第一个能命中的。
       2026-10-29 实测踩到: "八海鸟收纳箱大容量布艺…整理储物箱百纳箱…" 里,
       "储物箱" 排在关键词表前面 → 命中在第21字, 品牌被切成
       "八海鸟收纳箱大容量布艺衣服衣物被子衣柜整理" 这种半截词。
       正确做法是所有关键词里取**最小下标**。
    """
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    # 去掉【】促销角标, 那不是商品信息
    s = re.sub(r"[【\[]([^】\]]{0,24})[】\]]", r"\1", s)
    best_i, best_kw = -1, ""
    for kw in (keywords or []):
        i = s.find(kw)
        if i >= 0 and (best_i < 0 or i < best_i):
            best_i, best_kw = i, kw
    if best_i > 0:
        return _trim_brand(s[:best_i]), best_kw
    # 关键词在开头(商品名本身就是品类词) → 没有品牌, 用前缀兜底
    if best_i == 0:
        return _trim_brand(_brand_prefix(s, 16)), best_kw
    return _trim_brand(s), ""


def _spec_from_name(name: str, limit: int = 16) -> str:
    """从**真实商品名**里提取规格串。

    ⚠️ 只做提取不做编造 —— 规格必须和商品页上写的完全一致。

    ⚠️ 2026-10-29 实测踩到: 池子里的商品名**没有空格分隔**
       (如"京东京造高硼硅耐热玻璃保鲜盒微波炉饭盒便当盒家用饭盒六件套"),
       按 token 取会整段吞下商品名当规格, 文案里就出现
       "规格是京东京造高硼硅耐热玻璃保鲜盒微波炉饭盒便当盒" 这种废话。
    → 改成**只认"数字+单位"这种短片段**, 绝不吃整段 token。
    """
    s = re.sub(r"\s+", " ", str(name or "")).strip()
    out = []
    # 1) 阿拉伯数字 + 量词单位: 100L / 3层 / 24包 / 6件套 / 1把
    for m in re.findall(
            r"\d+(?:\.\d+)?\s*(?:包|抽|层|只|件|个|升|盒|筒|罐|瓶|支|条|枚|盘|套|组|档|格|把|厘|米|斤|克|毫升|ml|kg|cm|L|W)",
            s, re.I):
        out.append(m.replace(" ", ""))
    # 2) 中文数字 + 量词: 六件套 / 十只装 / 三层
    for m in re.findall(
            r"[一二三四五六七八九十]\s*(?:件套|只装|个装|件|只|个|包|支|盒|层|套|装|把)", s):
        out.append(m.replace(" ", ""))
    # 3) 尺寸串: W29.2×D19.4×H84.5
    for m in re.findall(r"[A-Z]?\d+(?:\.\d+)?\s*[×xX]\s*[A-Z]?\d+(?:\.\d+)?"
                         r"(?:\s*[×xX]\s*[A-Z]?\d+(?:\.\d+)?)?\s*[cms㎡]*", s):
        out.append(m.replace(" ", ""))
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    # ⚠️ 太长 → 从**最长**的片段开始丢, 留信息量最足的几段。
    #    (实测: "1把 W29.2×D19.4×H84.5" 直接塞进文案会挤掉整句)
    while len(" ".join(uniq)) > limit and len(uniq) > 1:
        uniq.pop(max(range(len(uniq)), key=lambda i: len(uniq[i])))
    return " ".join(uniq)[:limit]


# 品牌段里跟品牌无关的**材质/功能词** —— 在这些词处切断, 品牌才干净
_BRAND_CUT_MARKERS = (
    "高硼硅", "钢化玻璃", "耐热玻璃", "玻璃", "塑料", "木质", "木头", "不锈钢",
    "布艺", "绒布", "可折叠", "折叠", "加厚", "大容量", "多功能", "可伸缩",
    "伸缩", "家用", "厨房", "浴室", "卧室", "衣柜", "收纳", "整理", "后备箱",
    "车载", "翻盖", "抽屉", "内衣", "袜子", "文胸", "内裤", "衣服", "衣物",
    "被子", "扫把", "扫帚", "簸箕", "撮箕", "畚箕", "拖把", "垃圾铲", "清洁",
    "保鲜", "饭盒", "碗", "两用", "双层", "常规", "高性价比", "出口", "可旋转", "扫水器",
    "进口", "外贸", "旗舰", "官方", "专卖", "新品", "大号", "小号",
)


def _trim_brand(brand: str) -> str:
    """把品牌段修成**干净的品牌名**。

    ⚠️ 京东商品名是"品牌 + 材质 + 功能 + 品类"堆叠出来的, 直接取品类词
       之前的部分会带上材质词(2026-10-29 实测: "京东京造高硼硅耐热玻璃"、
       "乐扣乐扣（LOCK&LOCK）塑料密封"), 塞进文案读着像凑字。
    → 在括号、材质/功能词、数字处切断。
    """
    s = str(brand or "").strip()
    if not s:
        return s
    # 1) 括号里的英文名/系列名不是品牌主体: "悦卡（YUECAR）汽车后备箱" → "悦卡"
    if "（" in s:
        s = s.split("（")[0].strip()
    elif "(" in s:
        s = s.split("(")[0].strip()
    # 2) 在材质/功能词处切
    cut = len(s)
    for m in _BRAND_CUT_MARKERS:
        i = s.find(m)
        if i > 0:
            cut = min(cut, i)
    # 3) 在数字处切("佳帮手30cm大" → "佳帮手")
    dm = re.search(r"\d", s)
    if dm and dm.start() > 0:
        cut = min(cut, dm.start())
    if cut < len(s):
        s = s[:cut]
    s = s.strip(" ·-—、,，")
    # 4) 还太长且带空格 → 取第一段("欧拉象 OLA XIANG翻盖" → "欧拉象")
    if len(s) > 8 and " " in s:
        s = s.split(" ")[0].strip()
    return s or str(brand).strip()


# ============================================================
# 文案生成
# ============================================================

def build_copy(p: dict) -> str:
    """带货期文案: 50-100 字。痛点 → 方案 → 价格。全部事实来自输入。"""
    name = str(p.get("name") or p.get("title") or "").strip()
    short = _brand_prefix(name, 22)
    raw_price = str(p.get("price", "")).strip()
    pm = re.search(r"(\d+(?:\.\d+)?)", raw_price)
    price_num = f"¥{pm.group(1)}" if pm else raw_price
    cat = _first_cat(p.get("category", ""))
    desc = str(p.get("desc", "")).strip()

    groups = _pack(_units(desc), 32)

    parts = [f"{cat}党看过来，{short}。"]
    for g in groups:
        parts.append(g + "。")
    parts.append(f"现在到手{price_num}。")
    copy = "".join(parts)

    # 超长 → 从**尾部**丢正文组, 保住价格句(价格是带货期的事实核心)
    while len(copy) > DY_COPY_MAX and len(parts) > 2:
        parts.pop(-2)
        copy = "".join(parts)

    # 太短 → 补正文组
    idx = 0
    while len(copy) < DY_COPY_MIN and idx < len(groups):
        parts.insert(-1, groups[idx] + "。")
        idx += 1
        copy = "".join(parts)

    return copy


# 养号期文案的 4 种开头 —— 抖音图文"前三行定生死"
# ⚠️ 养号期**绝不能**出现"到手价/党看过来/现在下单"这类营销句式,
#    那是系统判定营销号的核心特征。新号要的是"好奇 + 干货 + 互动"。
WARMUP_OPENERS = (
    "说个可能有点反常识的事：",
    "90% 的人第一步就做错了：",
    "我后悔没早点知道这个：",
    "别再瞎买了，真的：",
)

# 养号期结尾的互动钩子 —— 评论权重比点赞高, 埋槽点提高评论率
WARMUP_HOOKS = (
    "你踩过这个坑吗？评论区说说。",
    "同款还有别的选择吗？想看的话评论区扣 1。",
    "还有更省的吗？评论区交流下。",
    "你会怎么选？评论区聊聊。",
)


def build_warmup_copy(p: dict, idx: int, tip: str = "") -> str:
    """养号期文案: 好奇钩子 + 干货 + 真实榜单事实 + 互动钩子。**不含价格**。

    ⚠️ 事实只用三样, 全部来自榜单页:
       榜单名次 / 评论条数 / 商品名里的规格。评论内容一概不写 —— 抓不到。
    """
    opener = WARMUP_OPENERS[idx % len(WARMUP_OPENERS)]
    hook = WARMUP_HOOKS[idx % len(WARMUP_HOOKS)]
    name = str(p.get("name") or p.get("title") or "").strip()
    cat = _first_cat(p.get("category", ""))
    brand, word = _split_brand(name, p.get("keywords") or [])
    spec = p.get("spec") or _spec_from_name(name)
    rank = p.get("rank")
    label = p.get("source_label") or ""
    reviews = p.get("reviews_text") or ""

    tip = tip or "挑这类东西，先看参数再看包装"
    parts = [f"{opener}{tip}。"]

    # 事实句
    fact = ""
    if brand and word:
        fact = f"比如{brand}{word}"
    elif brand:
        fact = f"比如{brand}"
    if rank and label:
        fact += f"，在京东「{label}」排行榜排第{rank}"
    if spec:
        fact += f"，规格是{spec}"
    if reviews:
        fact += f"，京东累计评价{reviews}"
    if fact:
        fact += "。"
        parts.append(fact)

    parts.append(hook)
    copy = "".join(parts)

    # 超长 → 砍事实句里的规格(最后加的, 信息密度最低)
    if len(copy) > DY_COPY_MAX:
        if spec:
            copy = copy.replace(f"，规格是{spec}", "")
        if len(copy) > DY_COPY_MAX:
            cut = copy[:DY_COPY_MAX]
            cut = cut.rsplit("。", 1)[0]
            copy = (cut or copy[:DY_COPY_MAX - 1]).rstrip("，,、 ") + "。"
    return copy


def _first_cat(category: str) -> str:
    """类目可能带 "·" 分隔的多级类目, 取第一段作为主类目。"""
    return re.split(r"[·|｜]", str(category or ""))[0].strip()


# ============================================================
# 出图 prompt
# ============================================================

def _color_en(hex_color: str) -> str:
    """hex → 英文色名描述(给 mmx prompt 用)。

    ⚠️ 不用固定色名字典 —— 池子里的颜色是从**真实主图**自动取的,
       固定字典覆盖不到, 会导致 prompt 说"白色"但图是米色 = 图文不符。

    ⚠️ 2026-10-29 实测踩到: #A7987A(米色布艺收纳箱) 被算成 "orange",
       mmx 直接画成**橙色塑料箱** → 图文不符 + 品类都错了。
       → 饱和度低的暖色必须落到 beige/tan, 不能笼统叫 orange。
    """
    h = str(hex_color or "#F2F2F2").lstrip("#")
    if len(h) != 6:
        return "light grey"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "light grey"
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == 0:
        return "black"
    sat = (mx - mn) / mx
    light = (mx + mn) / 2 / 255
    if mx == mn:
        return ("soft white" if light > 0.72
                else "deep black" if light < 0.22 else "neutral grey")
    if mx == r:
        hue = ((g - b) / (mx - mn)) % 6
    elif mx == g:
        hue = (b - r) / (mx - mn) + 2
    else:
        hue = (r - g) / (mx - mn) + 4
    hue *= 60
    if hue < 15 or hue >= 345:
        base = "red"
    elif hue < 45:
        base = "orange"
    elif hue < 70:
        base = "yellow"
    elif hue < 165:
        base = "green"
    elif hue < 200:
        base = "teal"
    elif hue < 260:
        base = "blue"
    elif hue < 290:
        base = "purple"
    else:
        base = "pink"
    # 低饱和暖色 → 米色系, 不能笼统叫 orange(会被画成塑料橙箱)
    if sat < 0.45 and base == "orange":
        base = "beige tan"
    elif sat < 0.45 and base == "yellow":
        base = "khaki beige"
    # 明度
    if light < 0.25:
        if sat < 0.18:
            base = "black"
        elif base in ("orange", "red", "yellow"):
            base = "dark brown"
        else:
            base = f"deep {base}"
    elif light > 0.78:
        base = "soft white" if sat < 0.12 else f"pale {base}"
    elif sat < 0.18:
        base = f"muted {base}"
    return base


def build_prompts(p: dict, stage: str) -> dict:
    """生成三图 prompt。

    ⚠️ 五个坑:
    1. **颜色必须与 color 字段一致**(10-26 踩坑: 卡片写黑、图是白 = 图文不符 = 假带货)
    2. cover 要"强视觉冲击"(产品特写/堆叠/大占幅), scene 要"使用场景"
    3. 不写任何文字进画面(AI 写中文必乱码), 文字全部后期 PIL 叠加
    4. ⚠️ **品牌名绝不能进 prompt** —— 2026-10-29 实测踩到:
       prompt 里写 "八海鸟收纳箱" → 模型把品牌当文字画到商品包装上,
       出图变成 "Hahdniao" 这种乱码, 商品表面全是鬼画符。
       → prompt 只用**英文品类词**(product_en); 品牌只出现在文案和尾图文字里。
    5. ⚠️ **背景不能跟着商品色一起染** —— 只写 "product color X" 时 mmx 会把
       整个画面刷成商品色, 商品和背景糊成一片。→ 明确写 "on a plain neutral background"。
    """
    noun = str(p.get("product_en") or "").strip()
    if not noun:
        noun = str(p.get("product_word") or "home storage product").strip()
    color = str(p.get("color", "#F2F2F2")).strip()
    cat = _first_cat(p.get("category", ""))
    color_en = _color_en(color)

    neg = ("absolutely no text, no letters, no characters, no numbers, no logo, "
           "no label, no writing, no symbols, no brand name, "
           "plain unmarked smooth surface")
    common = (f"of a {noun}, the product itself is {color_en} in colour, "
              f"standing on a plain neutral light grey background, "
              f"e-commerce minimalist product photography, soft natural daylight, "
              f"clean simple backdrop, {neg}, no watermark")

    cover = (f"Striking hero close-up {common}, "
             f"large product filling the frame, dynamic diagonal composition, "
             f"high contrast, eye-catching first impression, "
             f"vertical 3:4 format, premium commercial styling")

    scene = (f"Lifestyle usage scene {common}, "
             f"the {noun} placed in a realistic tidy everyday home situation, "
             f"soft focus background, natural props, warm authentic mood, "
             f"vertical 3:4 format, {neg}")

    # price 图由 PIL 生成, 这里给一个兜底 prompt(万一将来改成 AI 生成)
    price = (f"Clean minimal product backdrop {common}, "
             f"empty lower area for text overlay, soft studio lighting, "
             f"vertical 3:4 format, {neg}")

    return {"cover": cover, "scene": scene, "price": price}


# ============================================================
# 数据装载
# ============================================================

def _load_pool(slug: str) -> tuple:
    """载入垂直商品池 + 类目知识(tips/keywords)。

    返回 (products, meta):
        meta = {"vertical","category","fetched_at","tips_by_label","keywords_by_label"}
    """
    pool_path = os.path.join(POOL_ROOT, f"{slug}.json")
    if not os.path.exists(pool_path):
        print(f"❌ 找不到商品池 {pool_path}", file=sys.stderr)
        print("   先跑: python3 scripts/dy_pool.py", file=sys.stderr)
        return None, None
    pool = read_json(pool_path)

    tips_by_label, keywords_by_label, product_en = {}, {}, {}
    cfg_path = os.path.join(POOL_ROOT, "sources.json")
    if os.path.exists(cfg_path):
        for s in read_json(cfg_path).get("sources", []):
            tips_by_label[s.get("label", "")] = s.get("tips") or []
            keywords_by_label[s.get("label", "")] = s.get("keywords") or []
            product_en[s.get("label", "")] = s.get("product_en", "")

    return pool.get("products") or [], {
        "vertical": pool.get("vertical", ""),
        "category": pool.get("category", ""),
        "fetched_at": pool.get("fetched_at", ""),
        "tips_by_label": tips_by_label,
        "keywords_by_label": keywords_by_label,
        "product_en": product_en,
    }


def _pool_offset(arg: str, fetched_at: str, total: int) -> int:
    """商品池起始位置。

    ⚠️ **默认按日期自动轮转**: (今天 - 池子抓取日) × 5。
       这样每天自动换 5 个商品, 重跑同一天结果一致(不靠外部状态文件),
       也不会天天重复推前 5 个。
       ⚠️ 池子只有 40+ 个商品, 而 30 天养号要发 150 条 → 必须**定期重抓池子**:
          京东榜单页内容按关键词轮换, 重跑 dy_pool.py 就能拿到新商品。
    """
    if arg not in (None, "", "auto"):
        try:
            return max(0, int(arg)) % max(1, total)
        except ValueError:
            return 0
    if not fetched_at or not total:
        return 0
    try:
        base = datetime.strptime(fetched_at, "%Y-%m-%d").date()
    except ValueError:
        return 0
    today = datetime.strptime(resolve_date(None), "%Y-%m-%d").date()
    return ((today - base).days * 5) % total


def _load_picked(path: str) -> list:
    picked = read_json(path)
    if isinstance(picked, dict):
        picked = (picked.get("products") or picked.get("data", {}).get("products")
                  or [])
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--input", default=None, help="选品 json 路径(带货期用)")
    ap.add_argument("--pool", default=None,
                    help="垂直商品池 slug(养号期用), 如 home_storage")
    ap.add_argument("--pool-offset", default="auto",
                    help="商品池起始位置: auto=按日期自动轮转(推荐) / 整数=手动指定")
    ap.add_argument("--stage", choices=DY_STAGES, default=None,
                    help="账号阶段: warmup=养号期(不挂车不带价) / "
                         "monetize=带货期(挂车带价)")
    args = ap.parse_args()
    d = resolve_date(args.date)
    od = ensure_out_dir(d)

    # ---- 阶段 ----
    stage = args.stage or STAGE_WARMUP
    info = STAGE_INFO[stage]
    print(f"📌 阶段: {info['label']} ({stage})")
    print(f"   规则: {info['rule']}")
    print(f"   原因: {info['why']}\n")

    # ---- 取数 ----
    picked = []
    meta = {"vertical": "", "category": "", "fetched_at": "", "tips_by_label": {},
            "keywords_by_label": {}, "product_en": {}}
    if args.pool:
        picked, meta = _load_pool(args.pool)
        if picked is None:
            return 2
        off = _pool_offset(args.pool_offset, meta.get("fetched_at", ""), len(picked))
        if off:
            print(f"🔄 商品池轮转: 跳过前 {off} 个 (当日新商品)")
            picked = picked[off:] + picked[:off]
        print(f"🗂 垂直池: {args.pool} · {meta['vertical']} · "
              f"{len(picked)} 个商品 (取前 5)")
    else:
        src = args.input or os.path.join(od, "picked.json")
        if not os.path.exists(src):
            print(f"❌ 找不到选品文件 {src}", file=sys.stderr)
            print("   先跑: python3 scripts/dy_api.py --mock > output/{date}/picked.json",
                  file=sys.stderr)
            return 2
        picked = _load_picked(src)
        print(f"🗂 选品: {src} · {len(picked)} 个商品")

    if not picked:
        print("❌ 没取到任何商品", file=sys.stderr)
        return 2

    feed = []
    src_counter = {}      # 每个子类目已用几个 → tip 在**自己子类目**内轮转
    for i, p in enumerate(picked[:5], 1):
        name = str(p.get("name") or p.get("title") or "").strip()
        label = p.get("source_label", "")
        tips = meta["tips_by_label"].get(label) or []
        keywords = meta["keywords_by_label"].get(label) or p.get("keywords") or []
        product_en = (meta.get("product_en", {}).get(label)
                      or p.get("product_en") or "")
        sidx = src_counter.get(label, 0)
        src_counter[label] = sidx + 1
        # ⚠️ tip 必须取**该商品自己子类目**的干货, 不能按批次位置取。
        #    否则"保鲜盒怎么叠放"会配到一只扫把上, 读者一秒出戏。
        tip = tips[sidx % len(tips)] if tips else ""

        item = {
            "id": p.get("id", i),
            "stage": stage,
            "name": name,
            "category": p.get("category") or meta.get("category", ""),
            "desc": str(p.get("desc", "")).strip(),
            "color": p.get("color") or "#F2F2F2",
            "commission_rate": p.get("commission_rate"),
            "sales": p.get("sales"),
            "good_rate": p.get("good_rate"),
            "image": p.get("image", ""),
            "links": p.get("links") or {},
            # 榜单事实(养号期文案用)
            "rank": p.get("rank"),
            "source_label": label,
            "reviews_text": p.get("reviews_text", ""),
            "spec": p.get("spec") or _spec_from_name(name),
            "keywords": keywords,
        }
        brand, word = _split_brand(name, keywords)
        item["brand"] = brand
        item["product_word"] = word
        item["product_en"] = product_en
        # ⚠️ 出图 prompt 用**短名**, 不用完整 JD 商品名。
        #    完整名有 30+ 字("八海鸟收纳箱大容量布艺衣服衣物被子衣柜整理储物箱百纳箱"),
        #    塞进 mmx prompt 模型抓不住主体, 出图会跑成杂物堆。
        item["display_name"] = (f"{brand}{word}" if brand and word
                                else _brand_prefix(name, 22))
        # 养号期尾图要用的两行素材: 干货句 + 商品事实行
        item["tip"] = tip
        fact_bits = []
        if item["rank"] and label:
            fact_bits.append(f"京东「{label}」榜第{item['rank']}名")
        if item["spec"]:
            fact_bits.append(f"规格{item['spec']}")
        if item["reviews_text"]:
            fact_bits.append(f"累计评价{item['reviews_text']}")
        item["fact_line"] = " · ".join(fact_bits)

        # ---- 价格 ----
        # ⚠️ 带货期: 价格必须是真实数字, 解析不出来直接失败。
        #    养号期: 价格**不展示**, 池子里的商品本来就没有价格(榜单页抓不到),
        #    所以这里留空 —— 严禁为了凑字段编一个价格出来。
        raw_price = p.get("price")
        price_num = None
        if raw_price not in (None, ""):
            pm = re.search(r"(\d+(?:\.\d+)?)", str(raw_price))
            if not pm:
                if stage == STAGE_MONETIZE:
                    print(f"❌ 商品 {item['id']} 的 price 解析不出数字: {raw_price!r}",
                          file=sys.stderr)
                    return 1
            else:
                price_num = float(pm.group(1))
        if price_num is None and stage == STAGE_MONETIZE:
            print(f"❌ 带货期商品 {item['id']} 缺真实价格: {raw_price!r}", file=sys.stderr)
            return 1

        spec = re.sub(r"^.*?\d+(?:\.\d+)?", "", str(raw_price or "")).strip("()（）·,， ")
        if not spec:
            spec = str(p.get("price_note") or item["category"] or "")
        item["price"] = (f"¥{price_num:g}" + (f"({spec})" if spec else "")
                         if price_num is not None else "")
        item["price_num"] = price_num
        item["price_note"] = spec if price_num is not None else ""

        # ---- 文案 ----
        if stage == STAGE_WARMUP:
            item["copy"] = p.get("copy") or build_warmup_copy(item, i - 1, tip)
        else:
            item["copy"] = p.get("copy") or build_copy(item)

        item["image_prompts"] = p.get("image_prompts") or build_prompts(item, stage)
        feed.append(item)

    out = os.path.join(od, "feed.json")
    write_json(out, feed)
    print(f"📝 写入 {out} ({len(feed)} 个商品)")

    for item in feed:
        n = len(item["copy"])
        print(f"   [{item['id']}] copy {n} 字 | {item['copy'][:44]}...")

    # schema 断言: 失败直接 raise, 不让坏数据流到出图
    try:
        assert_feed(feed)
    except SystemExit:
        print("❌ feed 校验失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
