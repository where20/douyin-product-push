#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feishu_push.py — 抖音图文发布清单推送飞书

用法:
    python3 scripts/feishu_push.py <date>

读取 output/{date}/publish_manifest.json, 把「今天该发什么」推成一张飞书卡片:
    每个商品 = 1 张封面图(img_key) + 一段可直接复制的文案 + 榜单事实 + 链接

设计要点:
  - 凭证/webhook 路径与 xhs-product-push 支线同构 (同在 TimeTask/ 下):
      ~/.minimax/credentials/general/feishu.json  (appId/appSecret)
      <TimeTask>/config/env.json                 (feishu.webhook_url)
  - **软失败**: 推送失败只打警告并 exit 0 —— 产物(图/清单)已经生成好了,
    推送是附赠的, 不该让整条 cron 流水线看起来失败。
  - 绕开系统/环境代理 (本机残留死代理会让 urllib 100% Connection refused)。
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import out_dir, read_json, resolve_date  # noqa: E402

CRED_FILE = os.path.expanduser("~/.minimax/credentials/general/feishu.json")
# <TimeTask>/douyin-product-push/scripts/feishu_push.py → 上溯 3 层到 <TimeTask>
ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "env.json",
)

# 本机常驻死代理会让 urllib 全线 Connection refused, 强制空 ProxyHandler 直连
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen(req, timeout):
    return _OPENER.open(req, timeout=timeout)


def load_creds():
    if not os.path.exists(CRED_FILE):
        raise RuntimeError(f"凭证文件不存在: {CRED_FILE}")
    with open(CRED_FILE) as f:
        return json.load(f)


def load_webhook():
    if not os.path.exists(ENV_FILE):
        raise RuntimeError(f"env.json 不存在: {ENV_FILE}")
    with open(ENV_FILE) as f:
        env = json.load(f)
    url = ((env.get("feishu") or {}).get("webhook_url") or "").strip()
    if not url:
        raise RuntimeError("env.json 里没有 feishu.webhook_url")
    return url


def get_token(app_id, app_secret):
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        if data.get("code") != 0:
            raise RuntimeError(f"取 token 失败: {data}")
        return data["tenant_access_token"]


def upload_image(token, img_path):
    """上传图片到飞书图床, 返回 image_key。"""
    fname = os.path.basename(img_path)
    with open(img_path, "rb") as f:
        img_data = f.read()
    boundary = f"----FormBoundary{os.urandom(16).hex()}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image_type"\r\n\r\n'
        f"message\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + img_data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with _urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
        if result.get("code") != 0:
            raise RuntimeError(f"上传失败 {fname}: {result}")
        return result["data"]["image_key"]


def _compress(src, dst, max_w=720):
    """压到宽度 720 —— 飞书卡片展示用, 1080 没必要, 省带宽。"""
    try:
        from PIL import Image
    except ImportError:
        return src  # 没有 Pillow 就传原图
    try:
        im = Image.open(src).convert("RGB")
    except Exception:
        return src
    if im.width > max_w:
        im = im.resize((max_w, int(im.height * max_w / im.width)), Image.LANCZOS)
    im.save(dst, "JPEG", quality=85)
    return dst


def build_card(posts, date_str, stage, img_keys):
    """每个商品: 封面图 + 可直接复制的文案 + 榜单事实 + 链接。"""
    elements = []
    for p in posts:
        key = img_keys.get(p["id"])
        if key:
            elements.append({"tag": "img", "img_key": key, "alt": {"tag": "plain_text", "content": p.get("display_name") or p.get("name", "")[:20]}})
        text = (p.get("copy") or "").strip()
        block = f"**{p.get('display_name') or p.get('name', '')[:24]}**\n{text}"
        if p.get("fact_line"):
            block += f"\n📌 {p['fact_line']}"
        link = ((p.get("links") or {}).get("primary")) or ""
        if link:
            block += f"\n🔗 {link}"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
        elements.append({"tag": "hr"})

    tips = (f"📅 {date_str} · 养号期 · 共 {len(posts)} 组图文"
            f"\n🚫 不挂小黄车 · 不放第三方联系方式 · 记得勾选「作者声明:内容由AI生成」")
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": tips}]})

    title = f"📦 抖音图文清单 · {date_str}"
    if stage == "monetize":
        title = f"🛒 抖音带货清单 · {date_str}"

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "green"},
            "elements": elements,
        },
    }


def post_webhook(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(body)
    except Exception:
        raise RuntimeError(f"webhook 返回非 JSON: {body[:200]}")
    if data.get("code", 0) != 0:
        raise RuntimeError(f"webhook 拒绝: {data}")
    return data


def _manifest_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    date_str = resolve_date(argv[0] if argv else None)
    base = out_dir(date_str)
    manifest_path = os.path.join(base, "publish_manifest.json")
    if not os.path.exists(manifest_path):
        print(f"❌ 找不到发布清单: {manifest_path}", file=sys.stderr)
        return 0  # 软失败

    manifest = read_json(manifest_path)
    posts = manifest.get("posts") or []
    if not posts:
        print("⚠️ 清单里没有 posts, 跳过推送")
        return 0

    stage = (posts[0] or {}).get("stage") or "warmup"

    try:
        creds = load_creds()
        webhook = load_webhook()
    except Exception as e:
        print(f"⚠️ 飞书凭证读取失败, 跳过推送 (不影响已生成的产物): {e}", file=sys.stderr)
        return 0

    if dry_run:
        print(f"🧪 [dry-run] 日期={date_str} stage={stage} 商品={len(posts)}")
        print(f"   webhook: {webhook[:46]}...")
        print(f"   凭证 appId: {str(creds.get('appId'))[:12]}... ✅ 可读")
        for p in posts:
            local = os.path.join(base, f"dy_p{p['id']}_cover.jpg")
            ok = "✅" if os.path.exists(local) else "❌ 缺图"
            print(f"   {ok} {p.get('display_name', '')[:24]:<26} 文案 {len(p.get('copy') or '')} 字 · {p.get('fact_line', '')}")
        card = build_card(posts, date_str, stage, {p["id"]: "DRYRUN_IMG_KEY" for p in posts})
        print(f"   卡片元素数: {len(card['card']['elements'])} (dry-run 用假 img_key, 不上传)")
        print("   ✅ 凭证/清单/卡片构建链路 OK —— 未真实推送")
        return 0

    try:
        token = get_token(creds.get("appId"), creds.get("appSecret"))
    except Exception as e:
        print(f"⚠️ 取 token 失败, 跳过推送 (不影响已生成的产物): {e}", file=sys.stderr)
        return 0

    # ⚠️ 幂等: 清单没变就不重复推。
    #    2026-10-29 实测: 同一天连跑三次流水线 → 飞书收到 3 条一模一样的卡片, 很吵。
    stamp_path = os.path.join(base, ".feishu_pushed")
    mhash = _manifest_hash(manifest_path)
    if not force and os.path.exists(stamp_path):
        try:
            with open(stamp_path) as f:
                old = f.read().strip()
        except Exception:
            old = ""
        if old == mhash:
            print(f"⏭️  清单未变 (hash {mhash}), 跳过推送 (--force 可强推)")
            return 0

    tmp_dir = os.path.join(base, "_push_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    img_keys = {}
    try:
        for p in posts:
            local = os.path.join(base, f"dy_p{p['id']}_cover.jpg")
            if not os.path.exists(local):
                continue
            small = os.path.join(tmp_dir, f"cover_{p['id']}.jpg")
            img_keys[p["id"]] = upload_image(token, _compress(local, small))
    except Exception as e:
        print(f"⚠️ 图片上传失败, 跳过推送 (不影响已生成的产物): {e}", file=sys.stderr)
        return 0
    finally:
        try:
            for f in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, f))
            os.rmdir(tmp_dir)
        except Exception:
            pass

    try:
        post_webhook(webhook, build_card(posts, date_str, stage, img_keys))
    except Exception as e:
        print(f"⚠️ webhook 推送失败 (不影响已生成的产物): {e}", file=sys.stderr)
        return 0

    try:
        with open(stamp_path, "w") as f:
            f.write(mhash)
    except Exception as e:
        print(f"⚠️ 写推送标记失败 (下次可能重复推): {e}", file=sys.stderr)

    print(f"✅ 飞书推送完成: {len(posts)} 组图文 · {date_str} · stage={stage} · hash={mhash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
