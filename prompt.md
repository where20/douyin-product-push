# douyin-product-push — 抖音图文带货支线 · prompt 模板

> 面向每天跑的 agent。仓库约定见 `AGENTS.md`，这里只写**每日动作**。

## 任务

每天产出 5 个商品的**抖音图文带货稿**：3 张 3:4 竖版图 + 50-100 字文案 +
真实商品链接 + 图床 URL + 发布清单，供人工在抖音 App 挂车发布。

## 每日流程

### 1. 加载环境
```bash
source /Users/xiaoan/Documents/TimeTask/config/xhs.env.sh
export TASK_DY_DIR=/Users/xiaoan/Documents/TimeTask/douyin-product-push
cd $TASK_DY_DIR
TODAY=$(date +%Y-%m-%d)
```

### 2. 检查精选联盟凭证（缺失则走 mock，但要说明）
```bash
python3 scripts/dy_api.py --check
```
- 凭证齐备 → `--live` 真实拉取
- 凭证缺失 → `--mock`（用**已核验的真实商品**跑通链路），并在日志里注明
  「本次为 mock 数据，未接精选联盟」

### 3. 选品
```bash
python3 scripts/dy_api.py --live --category 日用百货 --size 5 > output/$TODAY/picked.json
```
选品标准（与 xhs 支线一致）：
- 价格 ¥10-500（冲动消费区，转化最高）
- 佣金率 15%-30%（太低不赚钱，太高易违规）
- 好评率 ≥95%
- **避开**：美妆（售后多）、食品（资质严）、高价 >200
- 5 个商品覆盖不同类目，别全选数码

### 4. 生成图文稿
```bash
python3 scripts/build_feed.py $TODAY
```
脚本自动生成：文案（痛点→方案→价格）、三图 prompt（颜色与 `color` 对齐）、
价格/佣金/链接。

⚠️ **人工复核 feed.json**：
- 文案里有没有编造的数字（库存紧张 / 已回购人数）→ 有就删
- 有没有违规词（最好 / 第一 / 绝对 …）
- `color` 与真实商品主色是否一致

### 5. 出图（15 张）
```bash
python3 scripts/gen_dy_images.py $TODAY
```
- cover / scene → mmx 出图（3:4，无文字）
- price → PIL 合成（中文价格必须精确，AI 写中文必乱码）

**必须 15/15 全绿**才能往下走。

### 6. 肉眼核对（别跳这步）
```bash
open output/$TODAY/dy_p1_cover.jpg   # 逐张看
```
- 颜色对不对（深色商品配浅色图 = 图文不符）
- 价格尾图的数字对不对
- 有没有 AI 生成的鬼画符文字

### 7. 图床上传 + 发布清单
```bash
python3 scripts/publish_pack.py $TODAY
```
产出 `publish_manifest.md` —— **照着它发**。

### 8. 写日志
`memory/{date}.md`：商品 / 佣金 / 数据源（live 还是 mock）/ 出图数 / 上传数 /
清单路径 / 下次注意。

---

## 发布规范（人工那一步）

1. 抖音 App → 左下角「+」→ 选「**图文**」
2. 按 **cover → scene → price** 顺序上传 3 张图
3. 点「**添加商品**」→ 从橱窗选对应商品（挂小黄车）
4. 粘贴 `publish_manifest.md` 里的文案
5. **勾选「作者声明：内容由 AI 生成」** ← 平台强制要求，不勾会被降权
6. BGM 选「轻快钢琴」或「城市清晨」类纯音乐
7. 发布时段：**07:00 / 12:30 / 20:00**

---

## 权限提醒（每次发布前确认）

| 挂车类型 | 有效粉丝 |
|---|---|
| 基础橱窗 | 0 粉（每周限挂 1 款，不能提现） |
| 直播 | 200 粉 |
| 短视频 | 500 粉 |
| **图文** | **1000 粉** |

不够粉就先用**普通图文**（不挂车）养号，发满 10 条原创作品 + 实名认证 + 500 元保证金。
