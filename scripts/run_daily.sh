#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# run_daily.sh — 抖音图文带货 · 养号期每日流水线 (cron 入口)
#
# 用法:
#   bash scripts/run_daily.sh              # 今天
#   bash scripts/run_daily.sh 2026-10-29  # 指定日期(补跑)
#
# 阶段: 固定 warmup(养号期)。切 monetize 前必须先过 GROWTH.md 的 5 条硬闸门。
#   0 粉新号不挂小黄车、不带价格、不放第三方联系方式 —— 这是断言强制的, 不是提示。
#
# 幂等:
#   - gen_dy_images 内部复用已存在的 _raw_*.jpg, 同日重跑不烧 mmx 额度
#   - publish_pack 先查 uploaded_urls.json 的 key 集合是否匹配当前 feed,
#     匹配且 URL 都活着 → --reuse-upload (不产生图床垃圾); 否则全量重传
#
# 失败分级:
#   exit 1 = 硬失败(产物不完整, 不可发布)
#   exit 0 = 软失败/降级(例: mmx 额度用尽 → 等下个窗口; 飞书推送失败)
set -uo pipefail

DATE="${1:-$(date +%F)}"
DIR="${TASK_DY_DIR:-/Users/xiaoan/Documents/TimeTask/douyin-product-push}"
STAGE="${DY_STAGE:-warmup}"
POOL="${DY_POOL:-home_storage}"

cd "$DIR" || { echo "❌ 进不去仓库目录: $DIR" >&2; exit 1; }

# mmx / 图床 / 飞书 都要直连, 本机常驻死代理会让请求全线失败
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy
export NO_PROXY="*" no_proxy="*"

echo "════════════════════════════════════════════"
echo "▶ 抖音图文流水线  日期=$DATE  阶段=$STAGE  池=$POOL"
echo "════════════════════════════════════════════"

STEP_FAILED=""
QUOTA_EXHAUSTED=0

# ---------- 1. 建 feed (选品 + 文案) ----------
echo ""
echo "【1/6】build_feed"
if ! python3 scripts/build_feed.py "$DATE" --stage "$STAGE" --pool "$POOL"; then
    echo "❌ build_feed 失败 —— 硬失败, 终止"
    exit 1
fi

# ---------- 2. 出图 (15 张 = 5 商品 x 3 角色) ----------
echo ""
echo "【2/6】gen_dy_images"
python3 scripts/gen_dy_images.py "$DATE"
rc=$?
if [ "$rc" = "3" ]; then
    echo "⚠️ mmx 额度用尽 —— 等下个窗口重置, 本次降级继续"
    QUOTA_EXHAUSTED=1
elif [ "$rc" != "0" ]; then
    echo "⚠️ 有图生成失败 (exit $rc) —— 继续, 由 verify_links 判定是否可发布"
fi

# ---------- 3. 上传图床 + 生成发布清单 ----------
echo ""
echo "【3/6】publish_pack"
# 判定能不能复用已有上传记录: key 集合要跟当前 feed 的 id 集合一致
REUSE=0
if [ -f "output/$DATE/uploaded_urls.json" ]; then
    if python3 - "$DATE" <<'PY'
import json, os, sys
d = sys.argv[1]
base = os.path.join("output", d)
try:
    feed = json.load(open(os.path.join(base, "feed.json")))
    up = json.load(open(os.path.join(base, "uploaded_urls.json")))
except Exception:
    sys.exit(1)
imgs = up.get("images") or {}
want = {str(p["id"]) for p in feed}
have = set(imgs)
if want != have:
    sys.exit(1)
need = len(feed) * 3
got = sum(len(v) for v in imgs.values())
sys.exit(0 if got >= need else 1)
PY
    then
        REUSE=1
    fi
fi

if [ "$REUSE" = "1" ]; then
    echo "♻️  已有上传记录与当前 feed 匹配, 复用"
    python3 scripts/publish_pack.py "$DATE" --reuse-upload || { echo "❌ publish_pack 失败"; exit 1; }
else
    python3 scripts/publish_pack.py "$DATE" || { echo "❌ publish_pack 失败"; exit 1; }
fi

# ---------- 4. URL 核验 (发布闸门) ----------
echo ""
echo "【4/6】verify_links"
python3 scripts/verify_links.py "$DATE"
rc=$?
if [ "$rc" != "0" ]; then
    echo "❌ URL 核验未过 —— 硬失败, 不要发"
    STEP_FAILED="verify_links"
fi

# ---------- 5. 推飞书 (软失败, 不影响产物) ----------
echo ""
echo "【5/6】feishu_push"
python3 scripts/feishu_push.py "$DATE" || echo "⚠️ 飞书推送失败, 不影响已生成的产物"

# ---------- 6. 写日志 + 提交推送 ----------
echo ""
echo "【6/6】写日志 + git"

export DY_RUN_DATE="$DATE" DY_RUN_STAGE="$STAGE" DY_RUN_POOL="$POOL" \
       DY_RUN_QUOTA="$QUOTA_EXHAUSTED" DY_RUN_STEPFAIL="$STEP_FAILED"
python3 - <<'PY'
import json, os
from datetime import datetime

d = os.environ.get("DY_RUN_DATE", "")
stage = os.environ.get("DY_RUN_STAGE", "")
pool = os.environ.get("DY_RUN_POOL", "")
quota = os.environ.get("DY_RUN_QUOTA") == "1"
stepfail = os.environ.get("DY_RUN_STEPFAIL", "")

base = os.path.join("output", d)
posts = []
try:
    posts = (json.load(open(os.path.join(base, "publish_manifest.json"))) or {}).get("posts") or []
except Exception:
    pass

status = "ok"
notes = []
if quota:
    status = "degraded"
    notes.append("mmx 额度用尽, 图形产物可能不完整 —— 等下个窗口重置后重跑")
if stepfail:
    status = "failed"
    notes.append(f"闸门未过: {stepfail} —— 不要发布")

lines = [
    f"# 抖音图文带货执行日志 · {d}",
    "",
    f"- 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    f"- 阶段: {stage}（养号期: 不挂小黄车 / 不带价格 / 不放第三方联系方式）",
    f"- 商品池: {pool}",
    f"- 状态: **{status}**",
    "",
    "## 今日产出",
    "",
]
for p in posts:
    lines.append(f"### {p.get('display_name') or p.get('name','')[:24]}")
    lines.append(f"- 榜单事实: {p.get('fact_line','')}")
    lines.append(f"- 文案: {p.get('copy','')}")
    lines.append(f"- 图: {len(p.get('images') or [])} 张 (cover / scene / price)")
    lines.append(f"- 链接: {(p.get('links') or {}).get('primary','')}")
    lines.append("")
lines.append(f"- 清单: output/{d}/publish_manifest.md")
lines.append(f"- 图床: cloudimgs.iepose.cn")
if notes:
    lines.append("")
    lines.append("## 注意事项")
    for n in notes:
        lines.append(f"- ⚠️ {n}")
lines.append("")
lines.append("## 踩坑")
lines.append("- (本日无新增)")
lines.append("")

os.makedirs("memory", exist_ok=True)
log_path = os.path.join("memory", f"{d}.md")
# ⚠️ 追加而不是覆盖: 同一天可能已经写过排查记录/阶段结论,
#    直接 "w" 覆盖会把那些内容抹掉(2026-10-29 实测: 372 行排查记录被覆盖成 44 行)。
existing = ""
if os.path.exists(log_path):
    with open(log_path) as f:
        existing = f.read()
mode = "a" if existing else "w"
sep = "" if not existing else (
    "\n\n---\n\n# 流水线执行日志 · "
    + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n\n")
with open(log_path, mode) as f:
    f.write(sep + "\n".join(lines))
print(f"   📝 写入 {log_path} (status={status}, {'追加' if existing else '新建'})")
PY

git add "output/$DATE" "memory/$DATE.md" 2>/dev/null
if git diff --cached --quiet; then
    echo "   无改动, 跳过 commit"
else
    git commit -q -m "dy-push: $DATE 抖音图文 ($STAGE) - $(python3 -c "
import json,os
try:
    p=json.load(open('output/$DATE/publish_manifest.json'))['posts']
    print(f'{len(p)} 组图文')
except Exception:
    print('清单生成')
")" && echo "   ✅ commit 完成"
fi

echo ""
echo "⬆️  push (走探测脚本, 判据看 SHA 区间行)"
bash push_with_probe.sh origin main 2>&1 | tail -5

echo ""
echo "════════════════════════════════════════════"
if [ "$QUOTA_EXHAUSTED" = "1" ]; then
    echo "⚠️ 完成(降级): mmx 额度用尽 —— 等窗口重置后重跑 bash scripts/run_daily.sh $DATE"
elif [ -n "$STEP_FAILED" ]; then
    echo "❌ 完成(失败): $STEP_FAILED —— 不要发布"
else
    echo "✅ 完成: 清单 output/$DATE/publish_manifest.md"
fi
echo "════════════════════════════════════════════"

[ -n "$STEP_FAILED" ] && exit 1
exit 0
