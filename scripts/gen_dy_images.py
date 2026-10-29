#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_dy_images.py — 生成抖音图文三图 (3:4 竖版)

用法:
    python3 scripts/gen_dy_images.py <date>
    python3 scripts/gen_dy_images.py 2026-10-29

产出 (output/{date}/):
    dy_p{pid}_cover.jpg   1080x1440  首图 —— 强视觉冲击, 决定点击率
    dy_p{pid}_scene.jpg   1080x1440  中图 —— 使用场景
    dy_p{pid}_price.jpg   1080x1440  尾图 —— 价格锚点(PIL 合成, 非 AI)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 为什么尾图用 PIL 而不是 mmx 生成
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**AI 生成的中文必乱码**。价格尾图的核心信息是「¥40.9」+「限时」这种中文文案,
让 mmx 画出来必然是鬼画符(2026 年在 FMDA 项目已实测踩过: 中文信息图 = HTML/PIL,
不能用 image-01)。

所以这里分工:
    cover / scene → mmx 生成(纯视觉, 无文字, 模型强项)
    price         → PIL 合成(中文价格文案必须精确, 模型弱项)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 抖音图文的三图不是随便 3 张图, 是有叙事分工的:
    cover(冲击) → scene(代入) → price(决策)
    对应短视频带货的"钩子→展示→逼单"结构。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (DY_IMG_HEIGHT, DY_IMG_WIDTH, DY_IMAGE_ROLES,  # noqa: E402
                     STAGE_INFO, STAGE_MONETIZE, STAGE_WARMUP,
                     ensure_out_dir, out_dir, read_json, resolve_date)

MMX_RATIO = "3:4"
AI_ROLES = ("cover", "scene")   # 这两个交给 mmx


def mmx_quota_check() -> dict:
    """出图前查 mmx 区间额度。

    ⚠️ 2026-10-29 实测: 额度打满时 mmx 报
       `已达到 Token Plan 用量上限`(HTTP 200, 错误藏在 body 里),
       重试又变成 `code 6 Network request failed` —— **看着像网络抖动,
       实际是配额耗尽**, 排查时极易走错路。
    → 批量出图前先探一次, 不够就**立刻**停。
       否则 15 张 × 3 次重试 = 45 次无效调用, 白等好几分钟。

    探不到就放行(体检失败不该挡住正事)。
    """
    try:
        r = subprocess.run(["mmx", "quota", "show", "--output", "json"],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(r.stdout)
    except Exception:
        return {}
    for m in (data.get("model_remains") or []):
        if m.get("model_name") == "general":
            return m
    return {}


def mmx_generate(prompt: str, out_path: str, retries: int = 2) -> bool:
    """调 mmx 出图。内部已处理代理, 外层别再加。

    ⚠️ **必须重试**: 2026-10-29 实测, 15 张批量出图里有 1 次 mmx 调用
       直接失败(瞬时接口抖动), 整批就 14/15 判失败, 白跑 5 分钟。
       → 失败自动重试(退避 4s/10s), 重试仍失败才算真失败。
    """
    cmd = ["mmx", "image", "generate", "--prompt", prompt,
           "--aspect-ratio", MMX_RATIO, "--out", out_path, "--quiet"]
    for attempt in range(retries + 1):
        if attempt:
            wait = 4 if attempt == 1 else 10
            print(f"   ↻ 第 {attempt} 次重试 (等 {wait}s) ...")
            time.sleep(wait)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            print("   ❌ mmx 超时")
            continue
        if r.returncode != 0:
            print(f"   ❌ mmx 失败: {(r.stderr or r.stdout)[:200]}")
            continue
        if os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
            return True
        print("   ❌ mmx 返回成功但文件缺失/过小")
    return False


def fit_1080x1440(im):
    """裁切+缩放到精确 1080x1440 (3:4), 居中裁剪不拉伸。"""
    from PIL import Image
    im = im.convert("RGB")
    target = DY_IMG_WIDTH / DY_IMG_HEIGHT
    w, h = im.size
    if w / h > target:
        nw = int(h * target)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / target)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    return im.resize((DY_IMG_WIDTH, DY_IMG_HEIGHT), Image.LANCZOS)


def find_cn_font() -> str:
    """找中文字体。优先环境变量, 退回 macOS/常见 Linux 路径。"""
    for p in (os.environ.get("FONT_CN"),
              "/System/Library/Fonts/STHeiti Light.ttc",
              "/System/Library/Fonts/PingFang.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if p and os.path.exists(p):
            return p
    return ""


def _fit_text(draw, text, font, max_width):
    """按**像素宽度**截断文字, 不是按字符数。

    ⚠️ 中文字符宽度差异极大(¥ 和 整 差几倍), 按字符数截断必然溢出边界。
    """
    if draw.textlength(text, font=font) <= max_width:
        return text
    out = ""
    for ch in text:
        if draw.textlength(out + ch, font=font) > max_width:
            break
        out += ch
    return out.rstrip("，。、· ") + ("…" if out else "")


def _wrap_text(draw, text, font, max_width, max_lines=2):
    """按**像素宽度**折行, 返回行列表。

    ⚠️ 只用 _fit_text 截断会把干货句砍成半句(2026-10-29 踩到:
       "收纳箱先量衣柜深度再挑容量…" 后面直接没了, 读者看到的是残句)。
    → 先折行, 放不下再在最后一行截断。
    """
    lines, cur = [], ""
    for ch in str(text or ""):
        if draw.textlength(cur + ch, font=font) <= max_width:
            cur += ch
            continue
        if cur:
            lines.append(cur)
        cur = ch
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if not lines:
        return []
    if len(lines) == max_lines:
        lines[-1] = _fit_text(draw, lines[-1], font, max_width)
    return lines


def compose_price_card(cover_path: str, dst: str, product: dict,
                       stage: str = STAGE_MONETIZE) -> bool:
    """尾图 = cover 打底 + 文案叠加 (PIL, 中文精确)。

    ⚠️ 叠加文案只用**产品事实**: 价格 / 卖点 / 规格。
       禁止写库存紧张、"最后 X 件"这类编造的紧迫感 —— 平台判虚假宣传会扣分。

    ⚠️ **养号期(stage=warmup)绝不显示价格** —— 价格是营销属性最强的信号,
       新号带价格会被判定为带货内容, 自然流量直接打折。
       养号期尾图改成"卖点/知识卡": 一句干货 + 引导看主页。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("   ❌ 缺少 Pillow")
        return False

    font_path = find_cn_font()
    if not font_path:
        print("   ❌ 找不到中文字体, 无法合成尾图")
        return False

    try:
        base = fit_1080x1440(Image.open(cover_path))
    except Exception as e:
        print(f"   ❌ 底图读取失败: {e}")
        return False

    # ---- 底部实心深色带(不是渐变) ----
    # ⚠️ 渐变会让上沿文字压在商品包装的印刷字上, 读不清。
    #    实心带 + 半透明深色 = 白字永远清晰可读。
    band_h = 400
    band_top = DY_IMG_HEIGHT - band_h
    base = base.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([(0, band_top), (DY_IMG_WIDTH, DY_IMG_HEIGHT)], fill=(0, 0, 0, 215))
    # 带子上沿做 40px 柔和过渡, 避免硬边
    for y in range(40):
        a = int(215 * (1 - y / 40))
        od.rectangle([(0, band_top - 40 + y), (DY_IMG_WIDTH, band_top - 39 + y)],
                     fill=(0, 0, 0, a))
    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)

    def font(sz):
        try:
            return ImageFont.truetype(font_path, sz)
        except Exception:
            return ImageFont.load_default()

    pad = 56
    max_w = DY_IMG_WIDTH - pad * 2
    y = band_top + 40

    if stage == STAGE_WARMUP:
        # ---- 养号期: 挑货知识卡, 无价格 ----
        # ⚠️ 大字是**干货句**(方法论), 小字是**商品事实**(榜单名次/规格/评价数)。
        #    干货句才是读者收藏的理由, 商品事实才是"这不是广告"的信任来源。
        f_chip = font(28)
        f_title = font(46)
        f_body = font(30)
        f_foot = font(28)

        chip = "挑货笔记"
        draw.text((pad, y), chip, font=f_chip, fill=(228, 228, 228))
        y += 58

        title = str(product.get("tip") or product.get("display_name") or "").strip()
        if title:
            for ln in _wrap_text(draw, title, f_title, max_w, max_lines=2):
                draw.text((pad, y), ln, font=f_title, fill=(255, 255, 255))
                y += 58
        y += 8

        body = str(product.get("fact_line") or "").strip()
        if body:
            for ln in _wrap_text(draw, body, f_body, max_w, max_lines=2):
                draw.text((pad, y), ln, font=f_body, fill=(200, 200, 200))
                y += 40

        y += 10
        draw.text((pad, y), "更多同类整理中 · 持续更新", font=f_foot,
                  fill=(160, 160, 160))
        base.convert("RGB").save(dst, "JPEG", quality=90, optimize=True)
        return True

    # ---- 带货期: 价格大字 + 名称 + 规格 ----
    f_price = font(92)
    f_name = font(38)
    f_note = font(32)

    raw_price = str(product.get("price", "")).strip()
    m = re.search(r"[¥￥]?\s*(\d+(?:\.\d+)?)", raw_price)
    price_big = f"¥{m.group(1)}" if m else raw_price[:12]
    spec = ""
    if m:
        spec = raw_price.replace(m.group(0), "").strip("()（）·,， ")
    if not spec:
        spec = str(product.get("price_note") or product.get("category") or "")

    draw.text((pad, y), price_big, font=f_price, fill=(255, 255, 255))
    y += 112

    # ---- 名称 ----
    # ⚠️ 用 display_name(品牌+品类短名), 不用完整 JD 商品名 ——
    #    完整名 30+ 字, 卡片上根本放不下, 截半句更难认。
    name = str(product.get("display_name") or product.get("name", ""))
    for ln in _wrap_text(draw, name, f_name, max_w, max_lines=2):
        draw.text((pad, y), ln, font=f_name, fill=(240, 240, 240))
        y += 50

    # ---- 规格/卖点 ----
    if spec:
        for ln in _wrap_text(draw, spec, f_note, max_w, max_lines=2):
            draw.text((pad, y), ln, font=f_note, fill=(190, 190, 190))
            y += 42

    base.convert("RGB").save(dst, "JPEG", quality=90, optimize=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--ignore-quota", action="store_true",
                    help="跳过 mmx 额度闸门(额度体检本身出问题时用)")
    ap.add_argument("--only", default=None,
                    help="只重出指定商品 id (如 100353738728), 其余跳过。"
                         "用于单张失败后补出, 不必整批重跑")
    ap.add_argument("--force", action="store_true",
                    help="强制重出已存在的图 (默认跳过已出好的图, 别白烧 mmx 额度)")
    args = ap.parse_args()
    d = resolve_date(args.date)

    feed_path = os.path.join(out_dir(d), "feed.json")
    if not os.path.exists(feed_path):
        print(f"❌ 找不到 {feed_path} —— 先写内容文件", file=sys.stderr)
        return 2
    feed = read_json(feed_path)
    od = ensure_out_dir(d)

    # 阶段: 从 feed.json 里第一个商品的 stage 字段继承(由 build_feed 写入)
    stage = (feed[0].get("stage") if feed else None) or STAGE_WARMUP
    info = STAGE_INFO[stage]
    print(f"📌 阶段: {info['label']} ({stage}) — {info['rule']}")
    print(f"   尾图内容: {'卖点/知识卡(无价格)' if stage == STAGE_WARMUP else '价格锚点'}\n")

    # ---- 额度闸门: 不够就立刻停, 别白烧 45 次重试 ----
    if not args.ignore_quota:
        q = mmx_quota_check()
        pct = q.get("current_interval_remaining_percent")
        if pct is not None and int(pct) <= 0:
            end = q.get("end_time")
            reset = ("%s 自动重置" % datetime.fromtimestamp(end / 1000).strftime("%H:%M")
                     if end else "下个窗口重置")
            print(f"❌ mmx 区间额度已用尽 (general 剩余 {pct}%), {reset}")
            print("   · 等重置     → 到点直接重跑本脚本")
            print("   · 立即继续   → platform.minimaxi.com 充值积分, 再重跑")
            print("   · 额度体检本身有问题 → 加 --ignore-quota 硬闯")
            return 3
        if pct is not None:
            print(f"🔋 mmx 区间额度剩余 {pct}%\n")

    ok = 0
    fail = 0
    for p in feed:
        pid = p["id"]
        if args.only and str(pid) != str(args.only):
            continue
        prompts = p.get("image_prompts") or {}
        cover_path = os.path.join(od, f"dy_p{pid}_cover.jpg")
        scene_path = os.path.join(od, f"dy_p{pid}_scene.jpg")
        price_path = os.path.join(od, f"dy_p{pid}_price.jpg")

        for role in AI_ROLES:
            prompt = prompts.get(role)
            if not prompt:
                print(f"❌ 商品 {pid} 缺 image_prompts.{role}")
                fail += 1
                continue
            raw = os.path.join(od, f"_raw_{role}_{pid}.jpg")
            final = cover_path if role == "cover" else scene_path
            # ⚠️ 幂等闸门 (2026-10-29 补):
            #    出图成功后 raw 会被删掉, 所以 mmx_generate 里的"文件已存在"检查
            #    永远不成立 → 每次复跑都会重新调 mmx 重出 15 张, 白烧额度。
            #    实测: 同一天连跑两次 = 30 张图烧掉。
            #    → 这里在调 mmx 之前先看成品图在不在, 在就跳过。
            #    (商品池按日期轮转, pid 每天换 → 文件名不同, 不会误跳别的商品)
            if (not args.force and os.path.exists(final)
                    and os.path.getsize(final) > 50000):
                kb = os.path.getsize(final) // 1024
                print(f"⏭️  {pid} / {role} 已存在 ({kb}KB), 跳过"
                      f"  [--force 可强制重出]")
                ok += 1
                continue
            print(f"🎨 {pid} / {role} ...")
            if not mmx_generate(prompt, raw):
                fail += 1
                continue
            try:
                from PIL import Image
                fit_1080x1440(Image.open(raw)).save(final, "JPEG", quality=90,
                                                    optimize=True)
            except Exception as e:
                print(f"   ❌ 缩放失败: {e}")
                fail += 1
                continue
            kb = os.path.getsize(final) // 1024
            print(f"   ✅ {os.path.basename(final)} ({kb}KB, "
                  f"{DY_IMG_WIDTH}x{DY_IMG_HEIGHT})")
            ok += 1
            if os.path.exists(raw):
                os.remove(raw)

        # 尾图: PIL 合成
        print(f"🎨 {pid} / price (PIL 合成) ...")
        if os.path.exists(cover_path) and compose_price_card(cover_path, price_path, p, stage):
            kb = os.path.getsize(price_path) // 1024
            print(f"   ✅ {os.path.basename(price_path)} ({kb}KB, "
                  f"{DY_IMG_WIDTH}x{DY_IMG_HEIGHT})")
            ok += 1
        else:
            fail += 1

    print(f"\n{'='*60}")
    print(f"出图完成: {ok} 成功 / {fail} 失败")
    if ok == 0:
        return 1
    # 半套图发出去 = 平台判低质 → 必须全绿
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
