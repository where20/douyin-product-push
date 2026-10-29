# douyin-product-push — 抖音图文带货支线

> 2026-10-29 新增。**与 `xhs-product-push` 平级独立**，两条支线互不影响。
> xhs 支线跑的是「京东选品 → 长图 → 飞书 → GitHub Pages 落地页」，
> 本支线跑的是「精选联盟选品 → 3:4 竖版三图 → 图文文案 → 发布清单」。

## 为什么不合并进 xhs 支线

1. **产物形态完全不同**：xhs 是 1080×15080 长图 + 5 切片；抖音是 1080×1440 竖版三图。
   强行合并会让 `compose_cards` 长出一堆 if-else。
2. **数据源完全不同**：xhs 走京东榜单页；抖音走精选联盟（需资质）。
   依赖不同、失败模式不同，绑一起会互相拖累。
3. **交付方式完全不同**：xhs 是落地页自动更新；抖音是**人工在 App 点发布**
   （挂车必须走 App 授权流程，API 发布要企业资质）。
4. **风险隔离**：抖音侧资质审核不通过 / 账号被限流，不应影响已稳定运行的 xhs 支线。

## 目录结构

```
douyin-product-push/
├── scripts/
│   ├── lib_dy.py         公共脚手架: 路径/日期/schema 断言/阶段常量/违规词
│   ├── dy_api.py         精选联盟选品接口 (--mock / --live)  → 带货期数据源
│   ├── dy_pool.py        养号期垂直商品池 (京东榜单页 → 真实商品)  → 养号期数据源
│   ├── build_feed.py     选品/商品池 → feed.json (文案 + 三图 prompt)
│   ├── gen_dy_images.py  出 15 张图 (cover/scene 用 mmx, 尾图用 PIL)
│   ├── publish_pack.py   图床上传 + 生成发布清单 (json/md)
│   ├── verify_links.py   全量 URL 核验闸门 (软失败/硬失败分级)
│   ├── feishu_push.py    推飞书卡片 (--dry-run 可只自检不真发)
│   └── run_daily.sh      一键流水线 (cron 入口, 含幂等判定与失败分级)
├── pool/
│   ├── sources.json      垂直定义: 榜单页 URL + 类目关键词 + 干货方法论
│   └── {slug}.json       抓下来的商品池 (真实 name/sku/名次/评论数/主图/主色)
├── output/{date}/
│   ├── picked.json       选品原始数据 (来自 dy_api)
│   ├── feed.json         图文稿 (内容源, 手工可改)
│   ├── dy_p{N}_{role}.jpg 15 张成品图
│   ├── uploaded_urls.json 图床真实 URL
│   └── publish_manifest.{json,md}  发布清单
├── GROWTH.md             30 天养号路线图 (人工操作手册)
└── memory/{date}.md      执行日志
```

## 两个阶段（2026-10-29 阶段化改造，最重要的地基）

| | 🌱 养号期 warmup | 💰 带货期 monetize |
|---|---|---|
| 粉丝门槛 | 0 – 1000 | ≥ 1000 有效粉 |
| 数据源 | `pool/{slug}.json`（垂直池） | `picked.json`（精选联盟） |
| 挂小黄车 | ❌ 绝对不挂 | ✅ |
| 文案/图带价格 | ❌ | ✅ |
| 第三方联系方式 | ❌ 前 7 天一律不放 | ❌ |
| 类目 | **必须单垂直** | 可适度放开，仍需垂直 |
| 尾图 | 挑货知识卡（干货句 + 榜单事实） | 价格锚点卡 |
| 脚本 | `--stage warmup --pool home_storage` | `--stage monetize` |

**为什么新号前 20 条不挂车**：系统默认「带货内容 = 营销内容」，新号一挂车，
自然流量池直接降一档，涨粉速度砍半 —— 等于主动告诉算法"我是广告号"。
**为什么前 7 天不导流**：出现微信号/其他平台名 = 违规降权，比不挂车还严重。

## 硬规则

1. **价格 / 佣金 / 销量 / 好评率 / 商品链接必须来自真实接口，严禁编造。**
   编造的链接 = 假带货链路，比没有链接更糟。每个 URL 都要 curl 核验 200/202。
2. **文案里的每个事实都要来自输入数据。**
   禁止写「库存只剩 237 件」「已有 5000 人回购」这类平台查不到的数字。
   平台会核查，虚假宣传 = 扣分 / 限流 / 封号。
3. **禁止抖音违规词**：最好 / 最佳 / 第一 / 顶级 / 极致 / 绝对 / 国家级 / 全网最低 /
   根治 / 治愈 / 假一赔十 / 纯天然 / 100%有效 ……（完整清单见 `lib_dy.py` 的 `BANNED_WORDS`）。
4. **`color` 只能是 `#RRGGBB`**，语义 = 商品主色。
   **出图 prompt 里的颜色词必须与 color 对齐** —— 卡片写黑、图是白 = 图文不符 = 假带货。
5. **AI 生成内容必须标注**：每条图文发布时要勾选「作者声明：内容由 AI 生成」。
   平台强制要求，不勾会被降权。
6. **价格尾图用 PIL 合成，不用 mmx 生成**。AI 写中文必乱码，
   尾图核心信息是「¥40.9」这种精确文案，只能后期叠加。
7. **不写按日期的脚本**。每天只写 3 个纯数据文件，代码长期不变。
8. **凭证走环境变量**（`DY_APP_KEY` / `DY_APP_SECRET` / `DY_ACCESS_TOKEN`），不写进代码。
9. **养号期（warmup）三条铁律**：
   - 不挂小黄车、不出现价格/下单/橱窗/包邮等营销信号（断言 `WARMUP_FORBIDDEN` 会拦）
   - **必须单垂直**（断言会拦跨类目的 feed）—— 类目一散账号标签就散，转粉率上不去
   - 不写任何第三方联系方式（前 7 天尤其）
10. **养号期商品池来自京东榜单页，没有价格也没有 desc 是正常的**，
    严禁为了凑字段编一个价格。断言阶段化：warmup 允许 price/desc 为空，
    monetize 强制 price 为真实数字且在 ¥10-500 主力带、desc ≥100 字。

## 抖音图文规范（2026-10-29 固化）

| 项 | 值 | 来源 |
|---|---|---|
| 图片比例 | 3:4 竖版 (1080×1440) | 平台图文推荐 |
| 每条图数 | 3 张 | 平台上限 9，实测 3 张转化最好 |
| 三图分工 | cover(冲击) → scene(代入) → price(决策) | 对应短视频「钩子→展示→逼单」 |
| 文案长度 | 50-100 字 | <50 判低质，>100 折叠 |
| 价格带 | ¥10-500 | 冲动消费区，转化最高 |
| 发布时段 | 07:00 / 12:30 / 20:00 | 流量池集中释放 |
| BGM | 轻快钢琴 / 城市清晨 | 纯音乐类 |

## 权限门槛（硬门槛，不是软建议）

| 挂车类型 | 有效粉丝 | 备注 |
|---|---|---|
| 基础橱窗展示 | 0 粉 | 每周限挂 1 个商品，不能提现 |
| 直播挂小黄车 | 200 粉 | |
| 短视频挂车 | 500 粉 | |
| **图文挂车** | **1000 粉** | 本支线目标形态 |

其他前置：实名认证（身份证正反面 + 人脸）→ 10 条公开原创作品 → 500 元保证金（可退）。

> 0 粉「试运营」通道：每周限挂 1 款，满 100 单后须补缴保证金，否则冻结权限。

## 资质申请清单

抖音商品数据**没有匿名可 curl 的公开接口**。三条路，按可行性排序：

### 路线 A：巨量百应达人授权（推荐，个人可走）
1. 抖音 App → 我 → 右上角 ☰ → 创作者服务中心 → 电商带货 → 商品橱窗
2. 实名认证 → 缴 500 元保证金 → 绑银行卡
3. 开通后进选品广场，选品 → 加入橱窗
4. **API 侧**：百应后台 OAuth 授权拿 `access_token`，
   调 `buyin/productSkus` 类接口拿分销商品（含佣金率）
- 门槛：个人可申请
- 产出：真实精选联盟商品 ID + 佣金率 → **挂车佣金链路能真正走通**

### 路线 B：精选联盟开放接口（团长定向邀约制）
- 仅向精选联盟服务商（团长）开放，个人申请不到
- 有团长权限后可申请自研接口
- 门槛高，但如果要做矩阵号，这是唯一规模化路径

### 路线 C：抖店开放平台（需商家授权）
- `openapi-fxg.jinritemai.com/product.detail`
- 需 app_key + app_secret + 签名 + access_token + **店铺授权**
- 适合已有抖店的商家

### 凭证落地方式
```bash
export DY_APP_KEY=...
export DY_APP_SECRET=...
export DY_ACCESS_TOKEN=...
python3 scripts/dy_api.py --check    # 体检
python3 scripts/dy_api.py --live --category 日用百货
```
凭证放 `~/.minimax/credentials/general/douyin.json`（与飞书凭证同目录，权限一致），
由 cron 前置检查加载，**不要提交进仓库**。

## 数据源可达性实测（2026-10-29）

| 端点 | 结果 | 说明 |
|---|---|---|
| `www.douyin.com` | ❌ HTTP 000 | curl 不通 |
| `haohuo.jinritemai.com/` | ❌ 200 但 0 字节 | 反爬空壳 |
| `buyin.jinritemai.com/` | ❌ 200 但是 SPA | 标题是「抖音小店入驻」，无商品数据 |
| `buyin.jinritemai.com/dashboard/servicehall/select-product` | ⚠️ 200 SPA | 选品广场，需登录态 |
| `openapi-fxg.jinritemai.com/product/detail` | ⚠️ 200 | 需完整签名 + 授权 |
| `op.jinritemai.com/docs/*` | ⚠️ 200 | **有 argus 反爬签名**，curl 拿不到正文 |
| `www.chanmama.com` | ⚠️ 200 | 第三方数据平台，需登录/付费 |
| `developer.open-douyin.com` | ⚠️ 200 | 文档站是 SPA，正文懒加载 |

## 已知坑

- **AI 生成中文必乱码** → 任何带文字的画面（价格卡、促销图）一律 PIL 合成。
- **mmx 出图默认 1:1**，抖音要 3:4 → 必须 `--aspect-ratio 3:4` + 居中裁切到 1080×1440。
- **curl 一律 `--noproxy '*'`**：本机代理端口时活时死，走代理反而拿不到。
- **macOS 没有 GNU `timeout`** → 用 `curl -m N`。
- **图床对同名文件再上传会重命名**（`{name}_{ts}_{n}`）→
  `published_urls.json` 必须存**接口返回的真实 URL**，不能拼模板。
  命名空间用 `douyin_{date}_p{N}_{role}.jpg`，不与 xhs 支线冲突。
- **半套图不能发**：出图必须 15/15 全绿才允许出发布清单，否则平台判低质。
- **mmx 区间额度是 5 小时滚动窗口，撑不住一天 15 张 × 多轮重跑**。
  2026-10-29 实测：出满 30+ 张后开始报
  `API error: 已达到 Token Plan 用量上限`（HTTP 200 但 body 是错误），
  后续重试还会变成 `code 6 Network request failed` —— **别被"网络错误"骗了**，
  根因是额度，不是网络。
  - 查额度：`mmx quota show`（看 `general` 的 `current_interval_remaining_percent`）
  - 恢复：等 `end_time` 到点自动重置（实测 5 小时窗口）
  - 策略：**分批出图**（一次 15 张就够，别整批反复重跑）+ 跑之前先 `mmx quota show`
  - 脚本已加**重试 + `--only <商品id>` 单张补出**，别再整批重跑耗额度
- **Pillow 12 把量化常量挪到了 `Image.Quantize.MEDIANCUT`**（老路径 `Image.quantize.MEDIANCUT`
  直接 AttributeError）。2026-10-29 实测：被 `except` 吞掉后，44 个商品主色**全部**静默回落成
  默认值，日志一行报错都没有 → 卡片颜色全错 = 图文不符 = 假带货。
  → 取色失败必须 `raise`，不做静默兜底。
- **京东榜单页没有价格**（`price-rmb` 恒空，JS 渲染），也没有 desc。养号期不展示价格，正好绕开。
- **京东榜单页内容会按关键词轮换** —— URL 名义上是"抽纸榜"实际可能返回别的品类，
  抓完必须逐条看真实商品名。
- **切句子时先剥掉残留标点**：2026-10-29 实测踩到 —— 对 `desc[:hook]` 的剩余文本分句时，
  分句正则会命中开头残留的句号 → 返回空串 → 文案少 10+ 字（46 字 < 50 字下限）被断言拦下。
  → 语义切分统一走 `_units()` / `_pack()`（先切句 → 再切子句 → 按预算打包），不手工切字符串。
- **商品名的品牌段要清洗**：京东名是"品牌+材质+功能+品类"堆叠的，直接取品类词之前的部分会带上
  "京东京造高硼硅耐热玻璃"这种材质词 → `_trim_brand()` 在括号/材质词/数字处切断。
- **规格串只认"数字+单位"短片段**：按 token 取会把整段商品名当规格
  （"规格是京东京造高硼硅耐热玻璃保鲜盒微波炉饭盒便当盒"）。
- **重抓商品池不能覆盖旧池子**（2026-10-29 为每周自动换血加的闸门）：
  榜单页改版 / 网络抖动会让抓取大面积失败，而取色失败的商品主色是 `null` ——
  这种残缺池子一旦写进磁盘，第二天的流水线在 `build_feed` 阶段直接崩。
  → `dy_pool.py` 现在会**剔除取色失败项**，有效商品 < 20 就**整体放弃本次重抓、
  保留旧池子**（exit 1）。失败时不要手动改 `pool/home_storage.json`，更不要重试刷。
- **执行日志只能追加，不能覆盖**（2026-10-29 实测踩到）：
  `run_daily.sh` 最初用 `open(..., "w")` 写 `memory/{date}.md`，
  把当天已写好的 372 行排查记录直接覆盖成 44 行流水线日志。
  → 同一天可能既有排查结论又有流水线日志，现在日志段用**追加模式**，
  并在中间插 `---` 分隔线。补跑历史日期时尤其注意。
- **出图原本不幂等，复跑会白烧额度**（2026-10-29 实测踩到）：
  `gen_dy_images.py` 出图成功后会把 `_raw_*.jpg` 删掉，导致 `mmx_generate` 里
  「文件已存在就复用」的检查**永远不成立** → 每次复跑都重新调 mmx 出 15 张。
  实测同一天连跑两次 = 30 张图额度烧光，还把 10:00→15:00 窗口打满。
  → 现在在调 mmx **之前**先看成品图（`final`）在不在，在就跳过；
  要重出用 `--force`（或删掉对应 jpg）。**cron 化后这条是刚需** ——
  否则每天定时任务失败重试一次就是 15 张额度。
- **飞书推送也要幂等**（2026-10-29 实测踩到）：
  同一天连跑三次流水线 → 飞书收到 3 条一模一样的卡片。
  → `feishu_push.py` 现在把清单的 sha256 写进 `output/{date}/.feishu_pushed`，
  清单没变就跳过推送。强制重推用 `--force`。该标记是本机状态，已 gitignore。

## 每日流程

### 一键（cron 实际走这条）

```bash
source /Users/xiaoan/Documents/TimeTask/config/xhs.env.sh
export TASK_DY_DIR=/Users/xiaoan/Documents/TimeTask/douyin-product-push
bash $TASK_DY_DIR/scripts/run_daily.sh          # 今天
bash $TASK_DY_DIR/scripts/run_daily.sh 2026-10-29   # 补跑指定日期
```

`run_daily.sh` 内部依次做：`build_feed` → `gen_dy_images` → `publish_pack` →
`verify_links` → `feishu_push` → 写 `memory/{date}.md` → git commit + push。

- **幂等**：`gen_dy_images` 先看成品图在不在，在就跳过（`--force` 可强制重出），
  同日重跑**不烧 mmx 额度**（实测复跑 0.7 秒跑完）；
  `publish_pack` 先比对 `uploaded_urls.json` 的 key 集合与当前 feed 是否一致，
  一致且图都在 → `--reuse-upload`，不产生图床垃圾。
- **失败分级**：exit 1 = 硬失败（产物不完整，别发）；exit 0 = 降级完成
  （例：mmx 额度用尽，等下个窗口重跑）。
- **日志追加而非覆盖**：同一天可能已写过排查记录，`memory/{date}.md` 用追加模式。

### 手动分步（排查时用）

```bash
source /Users/xiaoan/Documents/TimeTask/config/xhs.env.sh
export TASK_DY_DIR=/Users/xiaoan/Documents/TimeTask/douyin-product-push
cd $TASK_DY_DIR
TODAY=$(date +%Y-%m-%d)

# ---- 养号期 (0-1000 粉): 不挂车, 不带价格, 单垂直 ----
# 0) 商品池 (每周一次即可, 榜单页内容会轮换换血)
python3 scripts/dy_pool.py

# 1) 生成图文稿 (商品池按日期自动轮转, 每天换 5 个商品)
python3 scripts/build_feed.py $TODAY --stage warmup --pool home_storage

# 2) 出 15 张图 (cover/scene=mmx, 尾图=PIL 知识卡)
python3 scripts/gen_dy_images.py $TODAY

# 3) 图床上传 + 发布清单
python3 scripts/publish_pack.py $TODAY

# 4) URL 核验闸门 (发布前必过)
python3 scripts/verify_links.py $TODAY

# 5) 推飞书
python3 scripts/feishu_push.py $TODAY          # 加 --dry-run 只自检不真发

# 6) 人工在抖音 App 发布 (照 publish_manifest.md 操作, 不挂小黄车)

# ---- 带货期 (>=1000 粉): 挂车, 带价格 ----
python3 scripts/dy_api.py --live --category 日用百货 --out output/$TODAY/picked.json
python3 scripts/build_feed.py $TODAY --stage monetize
python3 scripts/gen_dy_images.py $TODAY
python3 scripts/publish_pack.py $TODAY
```

## 自动化调度（cron，2026-10-29 建）

用 **mavis 自带 cron**，不碰 macOS launchd/crontab（避免污染系统设置、卸载干净）。

| 任务 | 时间 | 做什么 |
|---|---|---|
| `douyin-warmup-daily` | 每天 **15:05** | 跑完整 warmup 流水线 + 推飞书 + commit/push |
| `douyin-pool-refresh` | 每周一 **09:00** | 重抓垂直商品池换血 + commit/push |

cron ID（管理用，`mavis cron` 工具按 ID 查/改/停）：

- `douyin-warmup-daily` → `2fbbbeeb-13de-454d-8e0b-88ea76a11c0c`
- `douyin-pool-refresh` → `ea9d1f0c-6feb-4874-88bd-1ac5ccf85bb6`

> 两个任务都用 `session.mode = new`（每次新对话），跟仓库里其它 cron 保持一致 —— 每日流水线互相独立，不累积上下文。

**为什么定 15:05**：mmx 是 5 小时滚动窗口，2026-10-29 实测 15:00 准时从 0% 重置到 99%。
新窗口刚开时出 15 张图最稳。`gen_dy_images.py` 内置额度闸门，不够会自动停并打重置时间，
不会白烧重试。
**为什么周一换血**：池子 44 个商品 / 每天 5 个 ≈ 8.8 天轮回一轮；京东榜单页内容会轮换，
重抓能换一批新品，避免 30 天养号期反复发同一批商品。

> `run_daily.sh` 是唯一入口 —— **不要让 agent 自己拆开跑各脚本**，幂等判定和失败分级都在里面。

## 自动化边界（说清楚，别误会）

**能自动化的**：选品、文案生成、三图出图、价格卡合成、链接核验、图床上传、发布清单。
**不能自动化的**：挂小黄车 + 发布 —— 需要达人账号授权态，平台未开放个人号图文发布 API。

所以这套东西的价值是：**把 90% 的重复劳动自动化，只留最后一次点击**。
等拿到资质 + 账号后，可以再补 `video.create` / 图文发布 API 那一层。

## 人工操作手册

`GROWTH.md` 是 30 天养号路线图：阶段日历、每日 SOP、数据看板指标、
阶段切换闸门（5 个硬条件）、已知坑速查。**改脚本前先看那份**。
