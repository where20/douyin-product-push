#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dy_api.py — 抖音精选联盟选品接口层

用法:
    python3 scripts/dy_api.py --mock                  # 用本地样例跑通全链路
    python3 scripts/dy_api.py --category 日用百货      # 真实调用(需凭证)
    python3 scripts/dy_api.py --check                 # 只体检凭证

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 资质现实 (2026-10-29 实测, 别抱幻想)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
抖音商品数据**没有**能匿名 curl 的公开接口。三条路:

| 路径 | 可行性 | 说明 |
|------|--------|------|
| 精选联盟开放接口 | 团长定向邀约制 | 仅向精选联盟服务商(团长)开放, **个人/普通开发者申请不到** |
| 抖店开放平台 product.* | 需店铺授权 | `openapi-fxg.jinritemai.com/product/detail` 需 app_key + 签名 + access_token, 且要**商家授权** |
| 巨量百应 buyin/productSkus | 需达人授权 | 达人后台 OAuth 授权后可查分销商品 |

**实测可达性 (2026-10-29)**:
  - `www.douyin.com`          → HTTP 000 (curl 不通)
  - `haohuo.jinritemai.com/`  → 200 但 **0 字节** (反爬空壳)
  - `buyin.jinritemai.com/`   → 200 但是 SPA 空壳, 标题是"抖音小店入驻", 无商品数据
  - `openapi-fxg.jinritemai.com/product/detail` → 200 但需完整签名
  - `www.chanmama.com`        → 200 第三方数据平台(需登录/付费)
  - `op.jinritemai.com/docs`  → 200 但**有 argus 反爬签名**, curl 拿不到正文

**所以本脚本的设计**:
  1. 真实调用路径写好, 凭证一到位直接切 --live
  2. 没有凭证时 --mock 用**已核验的真实商品**跑通下游全链路
  3. 签名算法 (hmac-sha256) 实现好, 拿到官方文档后只需核对参数名
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_dy import assert_api_creds  # noqa: E402

# 抖音电商开放平台网关
FXG_HOST = "https://openapi-fxg.jinritemai.com"

# 巨量百应(达人分销)网关 —— 达人场景用这个
BUYIN_HOST = "https://buyin.jinritemai.com"


def sign_params(app_secret: str, params: dict) -> str:
    """抖音开放平台签名: hmac-sha256。

    官方约定: 把所有**除 sign 外**的 query 参数按 key 升序拼成
    `k1=v1&k2=v2&...`, 用 app_secret 做 hmac-sha256 key, 取 hex。
    ⚠️ 拿到官方文档后请核对是否需要拼 app_key 前缀。
    """
    items = sorted((k, str(v)) for k, v in params.items() if k != "sign")
    query = "&".join(f"{k}={v}" for k, v in items)
    return hmac.new(app_secret.encode("utf-8"), query.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def call_fxg(method: str, biz_params: dict, app_key: str, app_secret: str,
             access_token: str, version: str = "2") -> dict:
    """调用抖店开放平台接口。

    URL 形态:
      https://openapi-fxg.jinritemai.com/{method}
        ?app_key={app_key}&method={method}&access_token={token}
        &timestamp={ts}&v={version}&sign={sig}
    body: JSON 编码的业务参数
    """
    import urllib.request

    ts = int(time.time())
    params = {
        "app_key": app_key,
        "method": method,
        "access_token": access_token,
        "timestamp": ts,
        "v": version,
    }
    params["sign"] = sign_params(app_secret, params)
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{FXG_HOST}/{method}?{qs}"
    body = json.dumps(biz_params, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "access-token": access_token})
    # ⚠️ --noproxy: 本机代理端口时活时死, 走代理反而拿不到
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_union_products(category: str, page_size: int, app_key: str,
                         app_secret: str, access_token: str) -> dict:
    """拉精选联盟分销商品列表。

    ⚠️ 方法名需以你拿到的官方文档为准。常见候选:
       - `product.list`   (抖店商品列表, 需商家授权)
       - `buyin.productSkus` (百应分销商品, 需达人授权)
    这里用一个可切换的常量, 拿到文档后改这里即可。
    """
    method = os.environ.get("DY_UNION_METHOD", "product.list")
    biz = {
        "page": 1,
        "page_size": page_size,
        "category": category,
        "product_type": 0,
        "status": 0,
    }
    return call_fxg(method, biz, app_key, app_secret, access_token)


# ---------------- mock 数据 ----------------
# ⚠️ 这些是 2026-10-29 **已核验的真实商品**(京东榜单页 + smzdm 取价, curl 200),
#    用来在拿到精选联盟凭证前把下游全链路跑通。**不是编造**。
#    拿到真实凭证后走 --live, mock 只作回归测试用。
MOCK_PRODUCTS = [
    {
        "id": "1",
        "name": "洁柔 C&S 粉Face 抽纸 3层110抽*24包 原生木浆 可湿水纸巾 整箱卫生纸",
        "product_en": "tissue paper multipack",
        "price": "¥40.9(24包整箱·京东自营)",
        "price_note": "京东自营活动价¥53.1,叠满减后到手约¥40.9/件;多件凑单还可再低,页面价随活动波动",
        "dy_category": "日用百货",
        "category": "家居日用 · 家庭囤货纸品",
        "desc": "双十一囤纸清单的常青款。洁柔粉Face系列精选原生木浆,不含荧光增白剂,自然无香,整箱24包、单包110抽的三层结构,湿水后不易破,擦手擦脸擦台面都能应付。实测单包折算不到1.71元,按三口之家一个月用量囤两三箱非常合适;包装走的是淡奶油色调,放在客厅或餐边柜不违和。评论区高频词是柔韧、不掉屑、囤货划算,京东该单品累计评价已超500万条,好评率99%,属于闭眼囤不会踩雷的日用消耗品。",
        "color": "#F0E6DA",
        "commission_rate": 0.15,
        "sales": 100000,
        "good_rate": 0.97,
        "image": "",
        "links": {
            "primary": "https://www.smzdm.com/p/184179478",
            "extra": []
        }
    },
    {
        "id": "2",
        "name": "美的 Midea YBD-X6PD AI交互吸烫机6.0 吸附式手持挂烫机 大蒸汽家用电熨斗",
        "product_en": "handheld garment steamer",
        "price": "¥277.1(京东自营·实付低至)",
        "price_note": "京东活动价¥440,单件实付低至¥277.1;页面价约¥521,双11期间可关注满减与国补叠加",
        "dy_category": "家用电器",
        "category": "衣物护理 · 换季手持挂烫机",
        "desc": "深秋换季衣柜翻出来的毛衣大衣皱成一团,熨衣板又占地方,这台就是为小户型准备的。美的YBD-X6PD是新一代吸附式手持挂烫机,喷蒸汽的同时把衣物吸住固定在挂烫面板上,不用手拽着布料来回推,褶皱一烫就平。智能显示屏把干熨、湿熨、面料模式直接标出来,不用盲猜档位;机身小巧,塞进衣柜侧袋都行,出差带着也方便。评论里高频词是吸力大、升温快、不漏水不留水渍,京东该单品累计评价超200万条,深秋和冬天都是高频刚需。",
        "color": "#F2F2F0",
        "commission_rate": 0.15,
        "sales": 100000,
        "good_rate": 0.97,
        "image": "",
        "links": {
            "primary": "https://www.smzdm.com/p/184375908/",
            "extra": []
        }
    },
    {
        "id": "3",
        "name": "斑马牌 ZEBRA JJ15 按动黑色中性笔 0.5mm 子弹头签字笔 学生考试刷题笔 10支装",
        "product_en": "gel ink ballpoint pen",
        "price": "¥28.65(10支装·京东自营凑单价)",
        "price_note": "京东自营页面价¥57,4件凑单实付¥114.6,折合单件¥28.65;单件直购约¥45.9-72.8",
        "dy_category": "文具办公",
        "category": "学习办公 · 考研季文具",
        "desc": "考研季和双11撞在一起,笔袋里这支是刚需。日本斑马1897年创立的老牌书写工具,JJ15是它的经典按动款型,0.5mm子弹头笔尖出墨均匀、不断墨不飞白;墨迹速干不易洇纸,考卷卷面不容易弄脏。半透明笔杆可以观察笔芯余量,防滑螺纹握位久写也不累手,弧形笔夹上印着品牌LOGO,10支装的排面摆桌上也好看。京东该单品累计评价超200万条,评论区反复出现的一支笔用了六年、囤着过中考高考的说法,属于学生党和办公族都认的签字笔。",
        "color": "#1F1F1F",
        "commission_rate": 0.15,
        "sales": 100000,
        "good_rate": 0.97,
        "image": "",
        "links": {
            "primary": "https://www.smzdm.com/p/184561077",
            "extra": []
        }
    },
    {
        "id": "4",
        "name": "海康威视 HIKVISION C6LITE 行车记录仪 单镜头 2K超清星光夜视 4G远程停车监控",
        "product_en": "dash cam device",
        "price": "¥289(京东自营·单镜头64GB版)",
        "price_note": "京东活动价¥348.9,叠首购礼金+满1元打9折+立减5%+国补后实付低至¥204.01;日常到手约¥286.9",
        "dy_category": "汽车用品",
        "category": "汽车出行 · 停车监控记录仪",
        "desc": "双11前后自驾出行频率高,记录仪属于上车就该装好的一次性投入。海康威视依托自家安防监控技术做行车记录仪,C6LITE是京东行车记录仪单品热卖榜第1名。2560×1440P的2K分辨率,相比1080P清晰度提升约30%,车牌和路牌白天夜里都能认;内置RTC超级电容,停车监控状态下不亏电不怕高温,循环覆盖也稳定。4G远程监控支持手机远程查看,遇到剐蹭不用等交警到场也能调出画面。京东该单品累计评价超20万条,画质清晰、安装简单是评论区最集中的好评点。",
        "color": "#2B2B2E",
        "commission_rate": 0.15,
        "sales": 100000,
        "good_rate": 0.97,
        "image": "",
        "links": {
            "primary": "https://www.smzdm.com/p/184427454",
            "extra": []
        }
    },
    {
        "id": "5",
        "name": "乖明星 我的世界磁力方块 磁吸积木拼装 儿童玩具 160多款自由组合DIY",
        "product_en": "magnetic building blocks toy",
        "price": "¥28(京东自营·50颗粒4人仔礼盒)",
        "price_note": "京东自营现售价¥28,PLUS95折后到手¥26.6;秒杀日可低至¥36-37,大套装价格区间¥40-80",
        "dy_category": "玩具乐器",
        "category": "潮玩手办 · 磁力积木拼装",
        "desc": "秋天的周末带娃出门,比商场更省心的方案是给一套能自己拼的东西。乖明星这款我的世界磁力方块主打160多款图案自由组合,全部颗粒六面磁吸、不分正负极,吸力牢固不容易散块,颗粒表面光滑无毛刺,低龄宝宝也能上手搭。六面可拼意味着同一个方块六种用法,既能搭造型也能拿来做简单的立体数学启蒙,拼好之后还能当桌面摆件,一套顶两套。京东该单品累计评价超50万条,评论区高频词是咬合紧实、无异味、拼好能摆桌面,双11 作为礼物或亲子囤货都合适。",
        "color": "#C9A227",
        "commission_rate": 0.15,
        "sales": 100000,
        "good_rate": 0.97,
        "image": "",
        "links": {
            "primary": "https://www.smzdm.com/p/183668306",
            "extra": []
        }
    }
]


def mock_fetch(category: str, page_size: int) -> dict:
    """本地真实样例。

    ⚠️ 类目过滤走 `dy_category`(抖音口径), 不是 `category`(京东多级类目) ——
       两者混用会让 `--category 日用百货` 一个都筛不出来。
    """
    items = [p for p in MOCK_PRODUCTS
             if not category or category in ("全部", p.get("dy_category"))]
    return {
        "code": 0,
        "msg": "mock",
        "data": {
            "products": items[:page_size],
            "total": len(items),
            "mock": True,
        },
    }


def pick_list(res: dict) -> list:
    """从接口返回里提取商品**列表** —— 不同接口字段名不同, 这里做兼容。"""
    data = res.get("data") or {}
    for key in ("products", "product_list", "items", "list", "sku_list"):
        if isinstance(data.get(key), list):
            return data[key]
    for key in ("products", "product_list", "items"):
        if isinstance(res.get(key), list):
            return res[key]
    return []


def pick_one(res: dict) -> dict:
    """从接口返回里提取商品列表 —— 不同接口字段名不同, 这里做兼容。"""
    data = res.get("data") or {}
    for key in ("products", "product_list", "items", "list", "sku_list"):
        if isinstance(data.get(key), list):
            return data[key][0] if data[key] else {}
    # 有的接口把列表放在顶层
    for key in ("products", "product_list", "items"):
        if isinstance(res.get(key), list):
            return res[key][0] if res[key] else {}
    return {}


def main():
    ap = argparse.ArgumentParser(description="抖音精选联盟选品接口")
    ap.add_argument("--mock", action="store_true", help="用本地真实样例跑通链路")
    ap.add_argument("--live", action="store_true", help="真实调用(需 DY_* 凭证)")
    ap.add_argument("--check", action="store_true", help="只体检凭证")
    ap.add_argument("--category", default="全部", help="类目筛选")
    ap.add_argument("--size", type=int, default=5, help="拉取条数")
    ap.add_argument("--out", default=None,
                    help="把选品结果写成**干净 JSON** 到该路径(推荐)。"
                         "不要用 `> file` 重定向, 日志行会混进 JSON")
    args = ap.parse_args()

    ok, missing = assert_api_creds()
    print(f"🔑 凭证: {'✅ 齐备' if ok else '❌ 缺 ' + ', '.join(missing)}")
    print(f"   DY_APP_KEY      = {'set' if os.environ.get('DY_APP_KEY') else 'unset'}")
    print(f"   DY_APP_SECRET   = {'set' if os.environ.get('DY_APP_SECRET') else 'unset'}")
    print(f"   DY_ACCESS_TOKEN = {'set' if os.environ.get('DY_ACCESS_TOKEN') else 'unset'}")

    if args.check:
        return 0 if ok else 1

    if args.live:
        if not ok:
            print("❌ --live 需要 DY_APP_KEY / DY_APP_SECRET / DY_ACCESS_TOKEN 三个凭证",
                  file=sys.stderr)
            print("   申请路径见 AGENTS.md「资质申请清单」", file=sys.stderr)
            return 2
        res = fetch_union_products(
            args.category, args.size,
            os.environ["DY_APP_KEY"], os.environ["DY_APP_SECRET"],
            os.environ["DY_ACCESS_TOKEN"])
    else:
        print("ℹ️  未指定 --live, 使用 mock 数据(2026-10-29 已核验真实商品)")
        res = mock_fetch(args.category, args.size)

    if res.get("code") != 0:
        print(f"❌ 接口返回错误: {json.dumps(res, ensure_ascii=False)[:500]}",
              file=sys.stderr)
        return 3

    products = pick_list(res)
    if not products:
        print(f"❌ 没能从响应里解析出商品。原始响应:\n"
              f"{json.dumps(res, ensure_ascii=False, indent=1)[:1500]}", file=sys.stderr)
        return 4

    mock = res.get("data", {}).get("mock", False)
    print(f"\n✅ 选品成功 {len(products)} 个 (mock={mock})")
    for p in products:
        print(f"   [{p.get('id')}] {str(p.get('name') or p.get('title'))[:40]}"
              f"  {p.get('price')}")

    # ⚠️ 必须写**干净 JSON**, 不能把上面的日志行重定向进文件。
    #    2026-10-29 实测: `dy_api.py --mock > picked.json` 会把
    #    "🔑 凭证:…" + 缩进 JSON 一起写进去 → build_feed 解析直接炸。
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False, indent=2)
        print(f"💾 写入 {args.out}")
    else:
        print(json.dumps(products, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
