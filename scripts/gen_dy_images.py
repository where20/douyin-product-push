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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (DY_IMG_HEIGHT, DY_IMG_WIDTH, DY_IMAGE_ROLES,  # noqa: E402
                     ensure_out_dir, out_dir, read_json, resolve_date)

MMX_RATIO = "3:4"
AI_ROLES = ("cover", "scene")   # 这两个交给 mmx


def mmx_generate(prompt: str, out_path: str) -> bool:
    """调 mmx 出图。内部已处理代理, 外层别再加。"""
    cmd = ["mmx", "image", "generate", "--prompt", prompt,
           "--aspect-ratio", MMX_RATIO, "--out", out_path, "--quiet"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        print("   ❌ mmx 超时")
        return False
    if r.returncode != 0:
        print(f"   ❌ mmx 失败: {(r.stderr or r.stdout)[:200]}")
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 5000


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


def compose_price_card(cover_path: str, dst: str, product: dict) -> bool:
    """尾图 = cover 打底 + 价格文案叠加 (PIL, 中文精确)。

    ⚠️ 叠加文案只用**产品事实**: 价格 / 卖点 / 规格。
       禁止写库存紧张、"最后 X 件"这类编造的紧迫感 —— 平台判虚假宣传会扣分。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("   ❌ 缺少 Pillow")
        return False

    font_path = find_cn_font()
    if not font_path:
        print("   ❌ 找不到中文字体, 无法合成价格尾图")
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

    f_price = font(92)
    f_name = font(38)
    f_note = font(32)

    pad = 56
    max_w = DY_IMG_WIDTH - pad * 2

    # ---- 价格: 大字 ----
    # ⚠️ 输入 price 可能是 "¥40.9(24包整箱·京东自营)" 这种长串,
    #    大字区只放**数字部分**, 规格放小字行 —— 否则字号被挤到看不清。
    raw_price = str(product.get("price", "")).strip()
    m = re.search(r"[¥￥]?\s*(\d+(?:\.\d+)?)", raw_price)
    price_big = f"¥{m.group(1)}" if m else raw_price[:12]
    spec = ""
    if m:
        spec = raw_price.replace(m.group(0), "").strip("()（）·,， ")
    if not spec:
        spec = str(product.get("price_note") or product.get("category") or "")

    y = band_top + 40
    draw.text((pad, y), price_big, font=f_price, fill=(255, 255, 255))
    y += 112

    # ---- 名称 ----
    name = str(product.get("name", ""))
    draw.text((pad, y), _fit_text(draw, name, f_name, max_w), font=f_name,
              fill=(240, 240, 240))
    y += 54

    # ---- 规格/卖点 ----
    if spec:
        draw.text((pad, y), _fit_text(draw, spec, f_note, max_w), font=f_note,
                  fill=(190, 190, 190))

    base.convert("RGB").save(dst, "JPEG", quality=90, optimize=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    args = ap.parse_args()
    d = resolve_date(args.date)

    feed_path = os.path.join(out_dir(d), "feed.json")
    if not os.path.exists(feed_path):
        print(f"❌ 找不到 {feed_path} —— 先写内容文件", file=sys.stderr)
        return 2
    feed = read_json(feed_path)
    od = ensure_out_dir(d)

    ok = 0
    fail = 0
    for p in feed:
        pid = p["id"]
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
        if os.path.exists(cover_path) and compose_price_card(cover_path, price_path, p):
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
