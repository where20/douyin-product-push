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
import math
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (DY_COPY_MAX, DY_COPY_MIN, DY_STAGES, OUT_ROOT, POOL_ROOT,  # noqa: E402
                     STAGE_INFO, STAGE_MONETIZE, STAGE_WARMUP,
                     assert_feed, detect_material, detect_name_color,
                     derive_product_en, ensure_out_dir, out_dir, read_json,
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


# 养号期文案的 8 种开头 —— 抖音图文"前三行定生死"
# ⚠️ 养号期**绝不能**出现"到手价/党看过来/现在下单"这类营销句式,
#    那是系统判定营销号的核心特征。新号要的是"好奇 + 干货 + 互动"。
# ⚠️ 2026-11-08 从 4 条扩到 8 条: 原来只有 4 条, 每天 5 个商品用 idx%4 取,
#    第 1 个和第 5 个必然撞同一个开头(实测 11-08 两条都是"说个可能有点反常识的事:"),
#    同一条图文里出现一模一样的起手式 = 观感上像批量生成。
WARMUP_OPENERS = (
    "说个可能有点反常识的事：",
    "90% 的人第一步就做错了：",
    "我后悔没早点知道这个：",
    "别再瞎买了，真的：",
    "有个细节，选之前真该看一眼：",
    "踩过坑才知道的关键点：",
    "先说结论，省得你白花冤枉钱：",
    "买之前我都会先看这一项：",
)

# 养号期结尾的互动钩子 —— 评论权重比点赞高, 埋槽点提高评论率
# ⚠️ 2026-11-08 同样从 4 条扩到 8 条, 理由同上。
WARMUP_HOOKS = (
    "你踩过这个坑吗？评论区说说。",
    "同款还有别的选择吗？想看的话评论区扣 1。",
    "还有更省的吗？评论区交流下。",
    "你会怎么选？评论区聊聊。",
    "你家里用的是哪种？评论区报个型号。",
    "这条能帮到你就点个赞，不想收藏也看看。",
    "还有别的坑吗？评论区补充一下。",
    "想看对比的话评论区扣个 2。",
)


def _fill_tip(tip: str, word: str = "", brand: str = "") -> str:
    """把干货句里的占位符替换成**这个商品自己的**词。

    ⚠️ 为什么要参数化 (2026-11-09):
       文案首句 = opener + 干货句。干货句原本按子类目写死, 于是同一子类目
       第 4 次被选中时, tip 循环回第 1 条 → 首句跟一周前那句一模一样
       (实测 60 条文案里首句只有 4 种高频, 各出现 9-12 次)。
       → 干货句里放 {word}(品类词), 运行时填成该商品**命中的那个关键词**
         (同一类目下不同商品命中不同词: 收纳箱/整理箱/收纳筐…),
         首句自然错开, 且句子的信息量没降。
    """
    out = str(tip or "")
    if "{word}" in out:
        out = out.replace("{word}", word or "这类东西")
    if "{brand}" in out:
        out = out.replace("{brand}", brand or "")
    # 品牌可能为空 → 去掉残留的标点/空格, 别让句子看着缺了一块
    out = re.sub(r"\s{2,}", " ", out).strip()
    out = re.sub(r"，\s*，", "，", out)
    out = re.sub(r"^\s*[，,、]\s*", "", out)
    return out


def build_warmup_copy(p: dict, idx: int, tip: str = "", day_offset: int = 0,
                      rank_fresh: bool = True) -> str:
    """养号期文案: 好奇钩子 + 干货 + 真实榜单事实 + 互动钩子。**不含价格**。

    ⚠️ 事实只用三样, 全部来自榜单页:
       榜单名次 / 评论条数 / 商品名里的规格。评论内容一概不写 —— 抓不到。

    ⚠️ day_offset: 按日期错开开头/结尾的取用位置。
       原来固定 idx%4 → 同一个位置的商品**永远**用同一句开头,
       连续几天看过去就是同一个账号在复制粘贴。
       加上日期偏移后, 隔天同一位置会换句式, 但当天 5 条内部仍然互不相同。

    ⚠️ rank_fresh: 名次过期就不写名次(见 _rank_is_fresh)。
       文案正文里的名次和尾图 fact_line 的名次必须**同时**受控 ——
       两处只要有一处漏掉, 就会出现"尾图不写名次但正文写榜第7名"的矛盾。
    """
    opener = WARMUP_OPENERS[(day_offset + idx) % len(WARMUP_OPENERS)]
    hook = WARMUP_HOOKS[(day_offset + idx) % len(WARMUP_HOOKS)]
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
    if rank and label and rank_fresh:
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
    # ⚠️ 2026-11-05 补: **近乎无彩色时色相是纯噪声**。
    #   #B2B4B6(不锈钢银灰) sat 只有 0.022, 但按 hue 算会落进 blue →
    #   输出 "muted blue", mmx 就画成蓝色箱子 = 图文不符。
    #   → 饱和度极低时只按明度区分 黑白灰, 不碰色相。
    if sat < 0.12:
        if light > 0.78:
            return "soft white"
        if light < 0.22:
            return "deep black"
        return "silver grey" if light > 0.55 else "charcoal grey"
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
    # ⚠️ 2026-11-05 补(11-04 记录的根因 #2, 之前漏了红轴):
    #   低饱和红轴没有兜底 → #463734(低饱和暗红) 落到 hue<15 → "red",
    #   但 light<0.25 又被压成 "dark brown" → 语义丢失。
    #   → 低饱和红一律走暖棕红, 别掉进"红=鲜艳红"的坑。
    elif sat < 0.45 and base == "red":
        base = "warm taupe brown"
    # 明度
    if light < 0.25:
        # ⚠️ 2026-11-05 补: **深色 + 低饱和时色相同样不可靠**。
        #   #16171C(悦卡后备箱, 实为黑灰) sat=0.21 → 按 hue 落进 blue →
        #   输出 "deep blue", mmx 会画成**蓝箱子** = 图文不符。
        #   → 深色区把 sat 阈值从 0.18 提到 0.35: 近黑但有色偏的, 一律 black。
        #   (高饱和深色不受影响: 藏青 #1B2A4A sat=0.64 / 深酒红 sat=0.66)
        if sat < 0.35:
            base = "black"
        elif base in ("orange", "yellow"):
            base = "dark brown"
        elif base == "red":
            # ⚠️ 别再一律 "dark brown" —— 红/棕/黄明度低时语义不同,
            #   塌成同一个词就分不出商品实际颜色了。
            base = "deep burgundy"
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
    # ⚠️ 优先用**商品名解析出的颜色语义**(如"米色"→beige, "玻璃"→clear
    #    transparent glass), 比从 hex 反推准得多; 没有才回退 _color_en。
    color_en = str(p.get("color_en") or "").strip() or _color_en(color)

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


# ============================================================
# 选品约束 —— 2026-11-08 加: 治"每天内容重复"
# ============================================================
# 2026-11-08 实测诊断出的三层重复:
#   1) **同款商品**: 池里 "吱凡日本收纳箱" 出现 3 次(完全同名不同 id),
#      "京东京造扫把簸箕套装" 3 次, "漫德莱扫把簸箕套装" 2 次 —— 同一天可能全上。
#   2) **子类目扎堆**: 池里只有 5 个子类目, 其中「收纳折叠整理箱+布储物箱」20 个、
#      「扫把套装组合+清洁工具簸箕」19 个 → 89% 是同两样东西。
#      原来直接切片取前 5 个, 天天都是"3 个收纳 + 2 个扫把", 观感是复制粘贴。
#   3) **跨天重复**: 44 个池 / 每天 5 个 = 8.8 天一轮, 30 天内每个商品都会
#      重复 2-4 次(实测 11-01 与 11-04 都用了悦卡收纳箱, 11-06 与 11-08 都用了吱凡)。
#
# → 改成**环形扫描 + 约束贪心**: 不再"切片取前 5", 而是把整个池子当候选,
#    按 (子类目配额 / 品牌配额 / 形态配额 / 历史已发) 过滤, 凑满 5 个为止。
#    起点仍由日期轮转决定(不靠外部状态文件, 重跑同一天结果一致)。


# 榜单名次的新鲜期: 超过这个天数没在榜上, 文案里就不写名次了。
# ⚠️ 3 周 = 21 天: 养号期一个月的跨度, 掉榜超过这个时间的商品名次已经不成立。
RANK_FRESH_DAYS = 21


def _rank_is_fresh(last_seen: str, today: str) -> bool:
    """该商品的榜单名次是否还新鲜(近期仍在榜上)。

    ⚠️ last_seen 为空 = 从没记录过(旧池子没有这个字段) → 视为不新鲜,
       不写名次 —— 宁可少一个钩子, 也不能写个可能已经过期的名次。
    """
    if not last_seen:
        return False
    try:
        ls = datetime.strptime(str(last_seen), "%Y-%m-%d").date()
        td = datetime.strptime(str(today), "%Y-%m-%d").date()
    except Exception:
        return False
    return (td - ls).days <= RANK_FRESH_DAYS


def _norm_name(name: str) -> str:
    """商品名归一化 —— 用于识别"完全同款不同链接"。

    去掉数字、英文、括号内容和所有分隔符, 只留中文字符序列。
    例: "吱凡日本衣服收纳箱家用布艺衣柜收纳盒衣物整理箱分层神器折叠储物盒"
        → "吱凡日本衣服收纳箱家用布艺衣柜收纳盒衣物整理箱分层神器折叠储物盒"
        (三个不同 id 归一化后相同 → 判为同款, 只留一个)
    """
    s = str(name or "")
    s = re.sub(r"[【】\[\]（）()《》]+", " ", s)
    s = re.sub(r"[0-9０-９]+", " ", s)
    s = re.sub(r"(?i)[A-Za-z]+", " ", s)
    s = re.sub(r"[\s,，、。·\-—+|/]+", "", s)
    return s


def _history_used_ids(exclude_date: str = "") -> set:
    """扫 output/*/feed.json, 收集**已经发过**的商品 id。

    ⚠️ 只当"优先跳过"的依据, 绝不当硬约束 —— 池子会被用完,
       用完还得继续发(养号期 30 天不能停), 所以选不满 5 个时自动放宽。
    """
    used = set()
    if not os.path.isdir(OUT_ROOT):
        return used
    for name in sorted(os.listdir(OUT_ROOT)):
        if exclude_date and name == exclude_date:
            continue
        path = os.path.join(OUT_ROOT, name, "feed.json")
        if not os.path.exists(path):
            continue
        try:
            data = read_json(path)
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for it in data:
            if isinstance(it, dict) and it.get("id"):
                used.add(str(it["id"]))
    return used


def _select_products(picked: list, meta: dict, exclude_ids: set = None,
                     n: int = 5, max_per_label: int = 2,
                     max_per_brand: int = 1, max_per_form: int = 2,
                     use_quota: bool = True, rotate: int = 0):
    """从候选池里挑 n 个商品, 尽量避开重复。

    约束(逐级放宽, 保证一定能凑满 n 个):
      严格轮: 跳过历史已发 + 同子类目配额 + 同品牌配额 + 同形态配额
      放宽轮: 允许用历史已发(池子不够了), 仍守配额
      全放宽: 只守"完全同款硬去重"

    ⚠️ 选的时候**按子类目轮转取**, 不是按池子顺序取 ——
       否则同一子类目的商品会连着排(收纳/收纳/收纳/扫把/扫把), 观感依然重复。

    ⚠️ rotate: 按日期轮转**子类目的出场顺序**(2026-11-09 加)。
       原来 group_order 是固定的 → 每天开头的 5 个子类目都一样
       (实测 10-29 ~ 11-06 连续 9 天都是"收纳折叠整理箱/布储物箱/清洁工具簸箕/
       扫把套装组合/保鲜盒6件套", 品类看着完全没变)。
       → 用 day_offset 旋转 group_order, 每天的起手品类就错开了,
         而每个子类目的**消耗速率不变**(一轮各取一个), 池子不会偏向大类。

    ⚠️ use_quota: 只有**养号期垂直池**才需要这些配额。
       带货期的商品来自精选联盟接口, 没有 source_label(全是空串),
       类目配额会变成"最多选 2 个" —— 直接把带货期打废。
       → 带货期走 use_quota=False, 只守硬去重。
    """
    exclude_ids = set(exclude_ids or ())
    keywords_by_label = meta.get("keywords_by_label") or {}

    # ---- 硬去重: 归一化后同名的只留一个 ----
    uniq, seen_norm = [], set()
    for p in picked:
        key = _norm_name(p.get("name", ""))
        if not key or key in seen_norm:
            continue
        seen_norm.add(key)
        uniq.append(p)

    # ---- 按子类目分组, 组内保持池子顺序 ----
    groups, group_order = {}, []
    for p in uniq:
        label = p.get("source_label", "")
        if label not in groups:
            groups[label] = []
            group_order.append(label)
        groups[label].append(p)

    # ⚠️ 2026-11-09: 按日期轮转出场顺序 —— 避免每天都是同一批子类目打头
    # ⚠️ 2026-11-09 追加: 光"轮转 1 格"还不够 —— 相邻子类目往往同属一个大类
    #    (收纳折叠整理箱 / 布储物箱 / 布衣收纳箱 / 衣物储物收纳箱 全是"收纳"家族),
    #    步长 1 时连续几天全落在同一家族 → 观众看到的是"今天 5 条有 4 条是收纳"。
    #    实测 25 天里有 19 天出现同家族 ≥3 条。
    #    → 改成**跨步铺开**: 步长取与类目总数互质的数(保证不重复遍历),
    #      一天内 5 个名额落在彼此隔开的位置上。
    if group_order and rotate:
        g_total = len(group_order)
        start = rotate % g_total
        stride = max(1, g_total // max(1, n))
        while stride < g_total and math.gcd(stride, g_total) != 1:
            stride += 1
        if stride >= g_total:
            stride = 1
        order, i = [], start
        for _ in range(g_total):
            order.append(group_order[i])
            i = (i + stride) % g_total
        group_order = order

    def _meta_of(p):
        label = p.get("source_label", "")
        kws = keywords_by_label.get(label) or p.get("keywords") or []
        brand, word = _split_brand(str(p.get("name") or ""), kws)
        # ⚠️ fallback 必须是**英文**形态词, 不能直接用中文 label ——
        #    中文 label 当形态会让"顺嘉利毛巾架置物架"这种字符串进 form 配额表,
        #    配额语义完全失效(而且形态名会漏进别的地方)。
        fb = (meta.get("product_en") or {}).get(label) or ""
        form = derive_product_en(str(p.get("name") or ""), fallback=fb)
        return label, brand, word, form

    chosen, chosen_ids = [], set()
    seen_norm2 = set()
    label_cnt, brand_cnt, form_cnt = {}, {}, {}
    notes = []

    for relax in (0, 1, 2):
        # 每个子类目组内从上次取到的位置继续
        ptr = {g: 0 for g in group_order}
        picked_here = 0
        while picked_here < n and any(ptr[g] < len(groups[g]) for g in group_order):
            progressed = False
            for g in group_order:          # ← 子类目轮转: 保证结构交错
                if picked_here >= n:
                    break
                if ptr[g] >= len(groups[g]):
                    continue
                p = groups[g][ptr[g]]
                ptr[g] += 1
                # ⚠️ 2026-11-09 根因修复: 指针前进了就算"有进展"。
                #    原来 progressed 只在**真的选中商品**时才置位, 于是:
                #      每轮扫 19 个组 → 各组第一个商品若全被"历史已发"跳过
                #      → picked_here 仍是 0 → progressed 保持 False → break
                #      → relax 轮次把**同样这 5 个商品**又选出来
                #    实际后果: 11-12 之后每天循环同一组 5 个商品(模拟实测),
                #    池子还有 96 个没用过也轮不到 —— 看着像池子不够, 其实是死循环。
                #    → 只要指针动了就继续扫, 直到挑满 n 个或全部扫完。
                progressed = True
                pid = str(p.get("id", ""))
                if pid in chosen_ids:
                    continue
                key = _norm_name(p.get("name", ""))
                if not key or key in seen_norm2:
                    continue
                label, brand, word, form = _meta_of(p)
                if not use_quota:
                    # 带货期: 只守硬去重, 不套养号期的配额
                    pass
                elif relax == 0:
                    if pid in exclude_ids:
                        notes.append(f"跳过 {pid}（历史已发）")
                        continue
                    if label and label_cnt.get(label, 0) >= max_per_label:
                        notes.append(f"跳过 {pid}（{label} 配额 {max_per_label} 已满）")
                        continue
                    if brand and brand_cnt.get(f"{label}|{brand}", 0) >= max_per_brand:
                        notes.append(f"跳过 {pid}（{label}/{brand} 配额 {max_per_brand} 已满）")
                        continue
                    if form and form_cnt.get(form, 0) >= max_per_form:
                        notes.append(f"跳过 {pid}（形态 {form} 配额 {max_per_form} 已满）")
                        continue
                elif relax == 1:
                    if label and label_cnt.get(label, 0) >= max_per_label + 1:
                        notes.append(f"跳过 {pid}（{label} 超额）")
                        continue
                chosen.append(p)
                chosen_ids.add(pid)
                seen_norm2.add(key)
                label_cnt[label] = label_cnt.get(label, 0) + 1
                bk = f"{label}|{brand}"
                brand_cnt[bk] = brand_cnt.get(bk, 0) + 1
                form_cnt[form] = form_cnt.get(form, 0) + 1
                picked_here += 1
                progressed = True
            if not progressed:
                break
        if len(chosen) >= n:
            break

    return chosen[:n], notes


def _pool_offset(arg: str, fetched_at: str, total: int) -> int:
    """商品池起始位置。

    ⚠️ **默认按日期自动轮转**: (今天 - 池子抓取日) × 5。
       这样每天自动换 5 个商品, 重跑同一天结果一致(不靠外部状态文件),
       也不会天天重复推前 5 个。
       ⚠️ 池子只有 40+ 个商品, 而 30 天养号要发 150 条 → 必须**定期重抓池子**:
          京东榜单页内容按关键词轮换, 重跑 dy_pool.py 就能拿到新商品。
       ⚠️ 2026-11-08 起改由 _select_products() 做跨天去重, offset 只决定**扫描起点**,
          不再直接决定"取哪 5 个"。
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
            print(f"🔄 商品池轮转: 扫描起点 {off} (不直接决定取哪 5 个)")
            picked = picked[off:] + picked[:off]
        print(f"🗂 垂直池: {args.pool} · {meta['vertical']} · "
              f"{len(picked)} 个商品候选")
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

    # ---- 选品约束: 去同款 + 子类目配额 + 跨天不重复 ----
    # ⚠️ 只有**养号期垂直池**才套这些约束:
    #    带货期商品来自精选联盟接口(没有 source_label, 每次都是当季好货),
    #    套养号期的子类目配额会直接打废它(所有商品 label 都是空串)。
    warmup = (stage == STAGE_WARMUP)
    used_ids = _history_used_ids(exclude_date=d) if warmup else set()
    if used_ids:
        print(f"🧾 历史已发商品: {len(used_ids)} 个 (优先跳过)")
    # ---- 日期偏移: 既用于选品时的子类目轮转, 也用于文案取句 ----
    # ⚠️ 2026-11-09: 选品类轮转也要用它, 所以必须**在选品之前**算出来
    try:
        day_offset = int(datetime.strptime(d, "%Y-%m-%d").strftime("%j"))
    except Exception:
        day_offset = 0
    # ⚠️ max_per_label=1: 当天**同一子类目不许出现两次**。
    #    原来默认 2 → 一天里两个"收纳箱类"是允许的, 观众看到的是重复题材
    #    (实测 25 天里 4 天出现当天子类目撞车)。
    #    放宽轮(relax=1)仍然允许第 2 个 —— 池子不够时的兜底, 不影响常态。
    selected, notes = _select_products(picked, meta, exclude_ids=used_ids, n=5,
                                       use_quota=warmup, rotate=day_offset,
                                       max_per_label=1 if warmup else 2)
    if notes:
        shown = notes[:8]
        for nt in shown:
            print(f"   · {nt}")
        if len(notes) > len(shown):
            print(f"   · …另外 {len(notes) - len(shown)} 条跳过原因省略")
    if len(selected) < 5:
        print(f"⚠️ 只选出 {len(selected)} 个商品 (池子不够/约束太紧)", file=sys.stderr)
        return 2
    labels = [p.get("source_label", "") for p in selected]
    print(f"🎯 选中 {len(selected)} 个 · 子类目分布: "
          f"{ {l: labels.count(l) for l in set(labels)} }")
    picked = selected

    feed = []
    src_counter = {}      # 每个子类目已用几个 → tip 在**自己子类目**内轮转
    for i, p in enumerate(picked[:5], 1):
        name = str(p.get("name") or p.get("title") or "").strip()
        label = p.get("source_label", "")
        tips = meta["tips_by_label"].get(label) or []
        keywords = meta["keywords_by_label"].get(label) or p.get("keywords") or []
        # ⚠️ 2026-11-05 根因修复: product_en 原来按**榜单类目**取,
        #    同一个"清洁工具簸箕"榜里混着扫把簸箕套装 / EVA地刮 / 硅胶刮水器,
        #    一律写成 "broom and dustpan set" → 出图形态全错 = 图文不符。
        #    → 优先从**真实商品名**解析形态+材质, 类目只当兜底。
        label_en = (meta.get("product_en", {}).get(label)
                    or p.get("product_en") or "")
        product_en = derive_product_en(name, fallback=label_en)
        # 商品名里写明了颜色 → 颜色语义直接用它(比 hex 反推准)
        named_color = detect_name_color(name)
        color_en_override = named_color.get("en") or ""
        if not color_en_override:
            # 材质自带典型色语义(如"玻璃"→clear transparent glass)
            mat = detect_material(name)
            color_en_override = mat.get("family_en") or ""
        if not color_en_override:
            # ⚠️ 2026-11-05 补: 字段必须和 **prompt 实际用的颜色语义同一个来源**。
            #   原来字段只填名色词/材质语义 → 布艺箱(无色词)字段是空串,
            #   而 build_prompts 里走的是 `_color_en(color)` 回落 —— 同一份数据两个来源,
            #   读 feed.json 的人会以为"这个商品没定色"。
            #   → 这里直接兜底成 _color_en(color); prompt 行为完全不变
            #     (它本来就是回落到 _color_en)。
            color_en_override = _color_en(p.get("color") or "#F2F2F2")
        sidx = src_counter.get(label, 0)
        src_counter[label] = sidx + 1
        brand, word = _split_brand(name, keywords)
        # ⚠️ tip 必须取**该商品自己子类目**的干货, 不能按批次位置取。
        #    否则"保鲜盒怎么叠放"会配到一只扫把上, 读者一秒出戏。
        # ⚠️ 2026-11-08: 加日期偏移 —— 原来恒取 tips[0], 同一个子类目每天
        #    第一条永远是同一句干货, 连续看就是复读。加上偏移后隔天会换。
        # ⚠️ 2026-11-09: 干货句扩到每类目 10 条 + {word} 参数化 ——
        #    原来每类目只有 3 条, 同类目第 4 次出现时 tip 必然循环回第 1 条,
        #    首句跟一周前完全一样(实测首句高频重复 9-12 次)。
        tip = tips[(day_offset + sidx) % len(tips)] if tips else ""
        tip = _fill_tip(tip, word=word, brand=brand)

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
            "last_seen": p.get("last_seen") or "",
            "reviews_text": p.get("reviews_text", ""),
            "spec": p.get("spec") or _spec_from_name(name),
            "keywords": keywords,
            "product_en": product_en,
            "color_en": color_en_override,
        }
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
        # ⚠️ 2026-11-08: 榜单名次只在该商品**近期还在榜上**时才写。
        #    池子改成累积模式后, 掉榜的旧商品会一直留在池里(last_seen 不更新),
        #    名次已经过期, 继续写"榜第7名"就是假事实 —— 平台核查起来是硬伤。
        #    → 过期的只写规格和评价, 事实照样有, 但不碰名次。
        if item["rank"] and label and _rank_is_fresh(item.get("last_seen"), d):
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
            item["copy"] = p.get("copy") or build_warmup_copy(
                item, i - 1, tip, day_offset,
                rank_fresh=_rank_is_fresh(item.get("last_seen"), d))
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
