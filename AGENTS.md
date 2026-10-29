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
│   ├── lib_dy.py         公共脚手架: 路径/日期/schema 断言/抖音规范常量
│   ├── dy_api.py         精选联盟选品接口 (--mock / --live)
│   ├── build_feed.py     选品数据 → feed.json (文案 + 三图 prompt)
│   ├── gen_dy_images.py  出 15 张图 (cover/scene 用 mmx, price 用 PIL)
│   └── publish_pack.py   图床上传 + 生成发布清单 (json/md)
├── output/{date}/
│   ├── picked.json       选品原始数据 (来自 dy_api)
│   ├── feed.json         图文稿 (内容源, 手工可改)
│   ├── dy_p{N}_{role}.jpg 15 张成品图
│   ├── uploaded_urls.json 图床真实 URL
│   └── publish_manifest.{json,md}  发布清单
└── memory/{date}.md      执行日志
```

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

## 每日流程

```bash
source /Users/xiaoan/Documents/TimeTask/config/xhs.env.sh
export TASK_DY_DIR=/Users/xiaoan/Documents/TimeTask/douyin-product-push
cd $TASK_DY_DIR
TODAY=$(date +%Y-%m-%d)

# 1) 选品 (有凭证走 --live, 否则 --mock)
python3 scripts/dy_api.py --live --category 日用百货 > output/$TODAY/picked.json

# 2) 生成图文稿 (文案 + 三图 prompt)
python3 scripts/build_feed.py $TODAY

# 3) 出 15 张图 (cover/scene=mmx, price=PIL)
python3 scripts/gen_dy_images.py $TODAY

# 4) 图床上传 + 发布清单
python3 scripts/publish_pack.py $TODAY

# 5) 人工在抖音 App 发布 (照 publish_manifest.md 操作)
```

## 自动化边界（说清楚，别误会）

**能自动化的**：选品、文案生成、三图出图、价格卡合成、链接核验、图床上传、发布清单。
**不能自动化的**：挂小黄车 + 发布 —— 需要达人账号授权态，平台未开放个人号图文发布 API。

所以这套东西的价值是：**把 90% 的重复劳动自动化，只留最后一次点击**。
等拿到资质 + 账号后，可以再补 `video.create` / 图文发布 API 那一层。
