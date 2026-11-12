#!/bin/bash
# push_with_probe.sh — 智能 push: 动态探测可用代理, 没有就直连
#
# 用法:
#   bash scripts/push_with_probe.sh [remote] [branch]
#   bash scripts/push_with_probe.sh origin main
#
# 为什么需要它(2026-10-21 实测)
# ------------------------------
# 本机 HTTP_PROXY/HTTPS_PROXY 指向的端口**会变, 而且有时有监听、有时没有**:
#
#   10-17  端口 7897  无监听 → 必须绕过代理, 直连才能推
#   10-20  端口 6696  无监听 → 同上
#   10-21  端口 6696  **有监听**(clash-verge 在跑) → **绕过反而推不上去**:
#          github.com 直连 TCP 443 直接超时, 经 6696 代理 0.75s 返回 200
#
# 所以两条旧规则都不够用:
#   ❌ "记住端口号"          → 端口会变
#   ❌ "一律显式绕过代理"    → 代理活着的时候, 绕过 = 连不上
#   ✅ **先探测, 再决定**     → 本脚本就是这个
#
# 探测顺序:
#   1. 环境变量 HTTP_PROXY / HTTPS_PROXY —— 端口有监听 且 经它访问 github.com 返回 200
#   2. 常见本地代理端口 7897 / 7890 / 7891 / 1080 / 10808 / 10809 —— 同样要实际探活
#   3. 都没有 → 直连(不设 http.proxy)
#
# 判据(不要只看 exit code):
#   1) 首选: push 输出里有 `main -> main` 或 `<sha>..<sha>` 区间行;
#   2) 兜底: 没有区间行时, **直接问远端真实的 main SHA**(SSH 优先, 失败退 HTTPS),
#      跟本地 HEAD 相等就算推成功了。
#      ⚠️ 为什么要兜底: "包已送达但 sideband 断连" 时输出是
#         "RPC failed / unexpected disconnect" + 重试的 "Everything up-to-date",
#         没有区间行, 但远端其实**已经收到了**(2026-11-12 实测)。
#         坏 pathspec 也会打 "Everything up-to-date" 却啥也没推 —— 所以必须问远端,
#         不能只看那句话, 也不能只比本地跟踪引用(SSH 层推送不更新它)。
set -u

REMOTE="${1:-origin}"
BRANCH="${2:-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 目标仓 = 当前目录所在 git 仓(优先, 这样父仓 TimeTask 也能直接复用本脚本);
# 当前目录不在 git 仓里时, 回退到脚本所在仓(xhs-product-push)。
if git rev-parse --git-dir >/dev/null 2>&1; then
  TASK_DIR="$(pwd)"
else
  TASK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

cd "${TASK_DIR}" || exit 1
echo "📁 目标仓: ${TASK_DIR} (${REMOTE}/${BRANCH})"

# --- 候选代理列表: 环境变量优先, 再加常见本地端口 ---
CANDIDATES=()
for v in "${HTTPS_PROXY:-}" "${HTTP_PROXY:-}" "${https_proxy:-}" "${http_proxy:-}"; do
  [ -n "${v}" ] && CANDIDATES+=("${v}")
done
for p in 7897 7890 7891 1080 10808 10809 6152 9090; do
  CANDIDATES+=("http://127.0.0.1:${p}")
done

# --- 探测: 端口有监听 + 经它访问 github.com 真能通 ---
pick_proxy() {
  local c hp host port code
  for c in "${CANDIDATES[@]}"; do
    hp="${c#*://}"; hp="${hp#*@}"; hp="${hp%%/*}"
    host="${hp%%:*}"; port="${hp##*:}"
    [ "${port}" = "${host}" ] && continue
    nc -z -G 2 "${host}" "${port}" >/dev/null 2>&1 || continue
    code="$(curl -x "${c}" -sS -o /dev/null -m 20 -w '%{http_code}' https://github.com/ 2>/dev/null)"
    if [ "${code}" = "200" ]; then
      echo "${c}"; return 0
    fi
  done
  return 1
}

PROXY="$(pick_proxy || true)"

# --- 第 2 层兜底: github.com 的某些 IP 段在本机被网络层挡住 ---
# ⚠️ 2026-11-09 实测: DNS 把 github.com 解析到 20.205.243.166, TCP 443 直接超时
#    (api.github.com 却正常 → 单域名路由问题, 不是本机网络坏了)。
#    换一个 GitHub 段位就通: 140.82.112.3 / 140.82.114.3 / 20.27.177.113 实测 200。
#    → 直连 push 失败时, 逐个探这些 IP, 用 http.curloptResolve 钉住能通的那个再推。
#    ⚠️ 用 -c 一次性传参, **不改仓库配置**, 更不改 /etc/hosts。
FALLBACK_IPS=(140.82.112.3 140.82.114.3 140.82.113.3 20.27.177.113 20.205.243.166)

pick_ip() {
  local ip code
  for ip in "${FALLBACK_IPS[@]}"; do
    # ⚠️ 2026-11-10 实测: 这里必须带 --noproxy '*'。
    #    否则 curl 走环境变量里的死代理 (127.0.0.1:6696) → 所有备用 IP 都返回 000,
    #    明明有通的路 (实测 140.82.114.3 → 200) 却被判成"都不通"。
    code="$(curl -sS --noproxy '*' -o /dev/null -m 12 -w '%{http_code}' \
            --resolve "github.com:443:${ip}" https://github.com/ 2>/dev/null)"
    if [ "${code}" = "200" ]; then
      echo "${ip}"; return 0
    fi
  done
  return 1
}

do_push() {
  # $1 = 代理(可空)  $2 = 钉住的 IP(可空)
  local proxy="$1" ip="$2"
  local -a opts=()
  if [ -n "${proxy}" ]; then
    opts+=(-c "http.proxy=${proxy}" -c "https.proxy=${proxy}")
  else
    # ⚠️ 2026-11-10 实测: "未探测到可用代理" 只说明**本地代理端口没监听**,
    #    但环境变量里的 HTTP_PROXY/HTTPS_PROXY 仍指向那个死端口,
    #    git 照样走它 → "Failed to connect to 127.0.0.1 port 6696"。
    #    → 真直连必须**双清**: env 变量 + git 配置(http.proxy 置空 = 禁用代理)。
    opts+=(-c "http.proxy=" -c "https.proxy=")
  fi
  [ -n "${ip}" ] && opts+=(-c "http.curloptResolve=github.com:443:${ip}")
  # ⚠️ macOS 自带 bash 3.2: 空数组在 set -u 下用 "${opts[@]}" 会报
  #    "unbound variable"。用 ${opts[@]+...} 兼容写法, 空数组时整个词消失。
  if [ -n "${proxy}" ]; then
    git ${opts[@]+"${opts[@]}"} push "${REMOTE}" "${BRANCH}" 2>&1 | tee /tmp/xhs_push.log
  else
    env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
      git ${opts[@]+"${opts[@]}"} push "${REMOTE}" "${BRANCH}" 2>&1 | tee /tmp/xhs_push.log
  fi
}

if [ -n "${PROXY}" ]; then
  echo "🔌 探测到可用代理: ${PROXY}"
  do_push "${PROXY}" ""
else
  echo "🔌 未探测到可用代理 → 直连推送"
  do_push "" ""
fi

# 直连/代理都失败 → 换 IP 段位再试
# ⚠️ 但先看是不是**本来就没东西可推**(远端已同步) —— 这种情况不用白等一轮探测
LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo '')"
REMOTE_SHA="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}" 2>/dev/null || echo '')"
if [ -n "${LOCAL_SHA}" ] && [ "${LOCAL_SHA}" = "${REMOTE_SHA}" ]; then
  echo "✅ 远端已是最新 (HEAD ${LOCAL_SHA:0:7} == ${REMOTE}/${BRANCH}) —— 无需推送"
  exit 0
fi

if ! grep -qE "${BRANCH} -> ${BRANCH}|^[0-9a-f]{7}\.\.[0-9a-f]{7}" /tmp/xhs_push.log; then
  IP="$(pick_ip || true)"
  if [ -n "${IP}" ]; then
    echo "🔁 直连未通 → 换 GitHub IP 段位重试: ${IP}"
    do_push "" "${IP}"
  else
    echo "⚠️ 备用 IP 段位也都不通 —— 判定为网络问题, 本次 push 失败"
  fi
fi

# --- 第 3 层兜底: SSH 通道 (2026-11-12 新增) ---
# ⚠️ 2026-11-12 实测: 某段时间 github.com 的 **443 全线不通**
#    (直连 000 + 5 个备用 IP 全部 000), 但 **22 端口照常通**,
#    `ssh -T git@github.com` 认证正常 → 用 SSH URL 再推一次就能过。
#    → 从 HTTPS remote URL 推导 SSH URL, 不改仓库 remote 配置(一次性传参)。
#    → 判据不变: 必须看到 SHA 区间行 / main -> main 行。
if ! grep -qE "${BRANCH} -> ${BRANCH}|^[0-9a-f]{7}\.\.[0-9a-f]{7}" /tmp/xhs_push.log; then
  REMOTE_URL="$(git remote get-url "$REMOTE" 2>/dev/null || echo '')"
  SSH_URL="$(printf '%s' "${REMOTE_URL}" | sed -E 's#^https?://github\.com/#git@github.com:#')"
  case "${SSH_URL}" in
    git@github.com:*)
      echo "🔗 443 全线不通 → 改走 SSH 通道: ${SSH_URL}"
      env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
        GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15" \
        git push "${SSH_URL}" "${BRANCH}:refs/heads/${BRANCH}" 2>&1 | tee /tmp/xhs_push.log
      ;;
    *)
      echo "⚠️ remote 不是 github.com HTTPS 地址, 跳过 SSH 兜底"
      ;;
  esac
fi

# --- 判据: 必须有 main -> main 或 SHA 区间行 ---
if grep -qE "${BRANCH} -> ${BRANCH}|^[0-9a-f]{7}\.\.[0-9a-f]{7}" /tmp/xhs_push.log; then
  echo "✅ push 确认成功(见上方 SHA 区间行)"
  exit 0
fi

# --- 兜底判据: 直接问远端"你到底收没收到" (2026-11-12 补实现) ---
# ⚠️ 坑: **包已送达但 sideband 连接断了** → push 输出
#    "RPC failed / send-pack: unexpected disconnect", 重试打印 "Everything up-to-date",
#    日志里既没有 SHA 区间行也没有 main -> main, 但**远端其实已经有这个 commit 了**。
#    (2026-11-12 15:30 实测: 脚本判 ❌, 用 api.github.com 核对 → 远端 main 已是该 SHA。)
#    坏的 pathspec 也会打同样的 "Everything up-to-date" 却啥也没推 —— 所以不能只看这句话。
#
# → 正确判据: **问远端真实的 main SHA**, 跟本地 HEAD 比。
#    ⚠️ 不能只比本地跟踪引用: 第 3 层 SSH 兜底是往 URL 推的
#       (git push <url> <branch>:refs/heads/<branch>), **不会更新** refs/remotes/origin/main
#       → 那条路径上跟踪引用永远停在旧值, 比相等会误判成失败。
#    → 优先走 SSH(443 可能全线不通时 22 端口照常通), 再退回 HTTPS。
#    → 确认成功后顺手把本地跟踪引用对齐, 让下次"远端已是最新"的早退逻辑生效。
probe_remote_sha() {
  local url="$1" out=""
  case "${url}" in
    git@github.com:*)
      out="$(GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15" \
             git ls-remote "${url}" "refs/heads/${BRANCH}" 2>/dev/null | awk 'NR==1{print $1}')"
      ;;
    https://*|http://*)
      if [ -n "${PROXY:-}" ]; then
        out="$(git -c "http.proxy=${PROXY}" -c "https.proxy=${PROXY}" \
               ls-remote "${url}" "refs/heads/${BRANCH}" 2>/dev/null | awk 'NR==1{print $1}')"
      fi
      if [ -z "${out}" ]; then
        out="$(env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
               git -c "http.proxy=" -c "https.proxy=" \
               ls-remote "${url}" "refs/heads/${BRANCH}" 2>/dev/null | awk 'NR==1{print $1}')"
      fi
      ;;
  esac
  printf '%s' "${out}"
}

LOCAL_SHA="$(git rev-parse HEAD 2>/dev/null || echo '')"
REMOTE_URL="$(git remote get-url "${REMOTE}" 2>/dev/null || echo '')"
SSH_URL="$(printf '%s' "${REMOTE_URL}" | sed -E 's#^https?://github\.com/#git@github.com:#')"

if [ -n "${LOCAL_SHA}" ]; then
  ACTUAL_SHA="$(probe_remote_sha "${SSH_URL}")"
  [ -z "${ACTUAL_SHA}" ] && ACTUAL_SHA="$(probe_remote_sha "${REMOTE_URL}")"
  if [ "${ACTUAL_SHA}" = "${LOCAL_SHA}" ]; then
    echo "✅ push 实际已成功: 远端 ${BRANCH} = ${LOCAL_SHA:0:7}"
    echo "   (无 SHA 区间行是因为 sideband 断连, 已向远端核对确认)"
    git update-ref "refs/remotes/${REMOTE}/${BRANCH}" "${LOCAL_SHA}" 2>/dev/null || true
    exit 0
  fi
  echo "   远端实际 ${BRANCH}: ${ACTUAL_SHA:-取不到(远端无此分支, 或网络不通)}"
fi

echo "❌ 未见 '${BRANCH} -> ${BRANCH}' 或 SHA 区间行, 且远端也没有本地 HEAD —— 判定为未推送成功, 请排查" >&2
echo "   本地 HEAD: ${LOCAL_SHA:-取不到} / 远端 ${REMOTE}/${BRANCH}: ${REMOTE_SHA:-取不到}" >&2
exit 1
