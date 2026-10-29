#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""publish_pack.py — 生成抖音发布包 (图床上传 + 发布清单)

用法:
    python3 scripts/publish_pack.py <date>

产出:
    output/{date}/uploaded_urls.json   图床真实 URL 映射
    output/{date}/publish_manifest.json  发布清单(每个商品: 3图URL + 文案 + 挂车信息)
    output/{date}/publish_manifest.md    人读版, 直接照着发

⚠️ 为什么要有 manifest:
    抖音图文发布**目前靠人工在 App 里操作**(挂车需要达人授权 + 橱窗权限),
    所以必须有一份"照着抄就能发"的清单。自动化的价值在于
    **把选品/出图/文案/链接核验/图床上传全部自动化**, 只留最后一步点击。

⚠️ 图床上传复用 xhs-product-push 的 cloudimgs 协议, 但命名空间独立
    (douyin_{date}_...), 不与小红书支线的图冲突。
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import (DY_IMAGE_ROLES, ensure_out_dir, out_dir, read_json,  # noqa: E402
                     resolve_date, write_json)

HOST = "https://cloudimgs.iepose.cn"
UPLOAD_PATH = "/api/upload-file"


def upload_one(local_path: str, custom_name: str) -> dict:
    """curl 上传(绕开 Python urllib 的 Cloudflare connection refused 问题)。"""
    cmd = ["curl", "--noproxy", "*", "-sS", "-m", "60",
           "-F", f"file=@{local_path};filename={custom_name}",
           HOST + UPLOAD_PATH]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=65)
        if r.returncode != 0:
            return {"success": False, "error": f"curl exit {r.returncode}"}
        data = json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "curl timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    if not data.get("success"):
        return {"success": False, "error": data.get("message", "unknown")}
    d = data["data"]
    return {
        "success": True,
        "filename": d["filename"],
        "size": d["size"],
        "url": HOST + d["url"].replace("/api/files/", "/api/images/"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=None)
    ap.add_argument("--reuse-upload", action="store_true",
                    help="复用已有 uploaded_urls.json, 不重新上传(图没变时用)")
    args = ap.parse_args()
    d = resolve_date(args.date)
    od = ensure_out_dir(d)

    feed_path = os.path.join(od, "feed.json")
    if not os.path.exists(feed_path):
        print(f"❌ 找不到 {feed_path}", file=sys.stderr)
        return 2
    feed = read_json(feed_path)

    # ---- 1. 上传 15 张图 (5商品 x 3图) ----
    urls = {}       # {pid: {role: url}}
    ok_n = 0
    fail_n = 0
    reuse_path = os.path.join(od, "uploaded_urls.json")
    if args.reuse_upload and os.path.exists(reuse_path):
        prev = read_json(reuse_path)
        urls = prev.get("images") or {}
        ok_n = sum(len(v) for v in urls.values())
        print(f"♻️  复用已有上传记录: {ok_n} 张 (--reuse-upload)")
    else:
        print(f"⬆️  上传 {len(feed) * len(DY_IMAGE_ROLES)} 张图到 cloudimgs ...\n")
        for p in feed:
            pid = p["id"]
            urls[pid] = {}
            for role in DY_IMAGE_ROLES:
                local = os.path.join(od, f"dy_p{pid}_{role}.jpg")
                if not os.path.exists(local):
                    print(f"   ❌ 缺文件 {os.path.basename(local)}")
                    fail_n += 1
                    continue
                remote_name = f"douyin_{d}_p{pid}_{role}.jpg"
                r = upload_one(local, remote_name)
                if r.get("success"):
                    urls[pid][role] = r["url"]
                    ok_n += 1
                    print(f"   ✅ {remote_name}  {r['url']}")
                else:
                    fail_n += 1
                    print(f"   ❌ {remote_name}  {r.get('error')}")

    write_json(os.path.join(od, "uploaded_urls.json"),
               {"date": d, "host": HOST, "images": urls,
                "uploaded": ok_n, "failed": fail_n})

    # ---- 2. 生成发布清单 ----
    manifest = []
    for p in feed:
        pid = p["id"]
        # 佣金率: 0.15 → 15% (小数值是接口原始值, 直接显示没意义)
        cr = p.get("commission_rate")
        cr_display = f"{float(cr) * 100:.0f}%" if cr is not None else "待接口填充"
        manifest.append({
            "id": pid,
            "name": p["name"],
            "price": p["price"],
            "category": p.get("category", ""),
            "copy": p["copy"],
            "images": [urls.get(pid, {}).get(r) for r in DY_IMAGE_ROLES],
            "image_roles": list(DY_IMAGE_ROLES),
            "links": p.get("links", {}),
            "commission_rate": cr,
            "commission_display": cr_display,
            # 抖音挂车需要商品ID(精选联盟), 资质到位后由 dy_api.py 填
            "dy_item_id": p.get("dy_item_id", ""),
            "posting": {
                "type": "image_text",
                "ratio": "3:4",
                "image_count": len(DY_IMAGE_ROLES),
                "suggested_times": ["07:00", "12:30", "20:00"],
                "need_ai_declaration": True,
                "steps": [
                    "抖音App → 左下角「+」→ 选「图文」",
                    "按 cover→scene→price 顺序上传 3 张图",
                    "点「添加商品」→ 从橱窗选对应商品(挂小黄车)",
                    "粘贴文案 → 勾选「作者声明:内容由AI生成」",
                    "选 BGM(轻快钢琴/城市清晨) → 发布",
                ],
            },
        })

    write_json(os.path.join(od, "publish_manifest.json"),
               {"date": d, "count": len(manifest), "posts": manifest})

    # ---- 3. 人读版 markdown ----
    lines = [f"# 抖音图文发布清单 · {d}", "",
             f"> {len(manifest)} 条图文待发布。图床上传 {ok_n} 张, 失败 {fail_n} 张。", "",
             "> ⚠️ 每条都要勾选「作者声明:内容由AI生成」—— 平台要求 AI 内容必须标注。", "",
             "> ⚠️ 挂车需要抖音橱窗权限: 图文挂车 **1000 有效粉**; 0 粉只能开基础橱窗(每周限挂 1 款)。", ""]
    for m in manifest:
        lines += [f"---", "", f"## [{m['id']}] {m['name']}", "",
                  f"**价格**: {m['price']}  |  **类目**: {m['category']}  |  "
                  f"**佣金**: {m['commission_display']}", "",
                  "**文案** (直接复制):", "", f"```\n{m['copy']}\n```", ""]
        for i, (role, u) in enumerate(zip(m["image_roles"], m["images"]), 1):
            role_cn = {"cover": "首图(强视觉冲击)", "scene": "中图(使用场景)",
                       "price": "尾图(价格锚点)"}.get(role, role)
            lines.append(f"{i}. **{role_cn}**: {u or '❌ 上传失败, 需重传'}")
        lines += ["", "**商品链接**:", ""]
        for k, v in (m["links"] or {}).items():
            if k == "extra":
                for u in v:
                    lines.append(f"- {u}")
            else:
                lines.append(f"- {v}")
        lines += ["", "**发布步骤**: " + " → ".join(m["posting"]["steps"]), ""]

    md_path = os.path.join(od, "publish_manifest.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n{'='*60}")
    print(f"📦 发布清单: {md_path}")
    print(f"   JSON: {os.path.join(od, 'publish_manifest.json')}")
    print(f"   图床: {ok_n} 成功 / {fail_n} 失败")
    return 0 if fail_n == 0 and ok_n == len(feed) * len(DY_IMAGE_ROLES) else 1


if __name__ == "__main__":
    sys.exit(main())
