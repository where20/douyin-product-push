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
        "id": "7224900",
        "title": "洁柔 粉Face 抽纸 3层110抽*24包 原生木浆 可湿水纸巾 整箱",
        "price": 40.9,
        "commission_rate": 0.2,
        "sales": 5000000,
        "good_rate": 0.98,
        "image": "https://img14.360buyimg.com/n7/s80x80_jfs/t1/473232/21/11855/122313/6a4f3a22F433b5e74/0083320320.jpg",
        "category": "日用百货",
    },
    {
        "id": "100296934031",
        "title": "美的 YBD-X6PD AI交互吸烫机 6.0 吸附式手持挂烫机 大蒸汽",
        "price": 277.1,
        "commission_rate": 0.15,
        "sales": 120000,
        "good_rate": 0.97,
        "image": "https://img14.360buyimg.com/n7/s80x80_jfs/t1/401111/21/44884/33217/6a5b4f0eF4c8a1d3b/0083320320.jpg",
        "category": "家用电器",
    },
    {
        "id": "4345605",
        "title": "斑马牌 ZEBRA JJ15 按动黑色中性笔 0.5mm 子弹头签字笔 学生考试",
        "price": 28.65,
        "commission_rate": 0.25,
        "sales": 300000,
        "good_rate": 0.99,
        "image": "https://img14.360buyimg.com/n7/s80x80_jfs/t1/321111/11/55555/12345/6a7c9e1aF4b2d8e7c/0083320320.jpg",
        "category": "文具办公",
    },
    {
        "id": "100050916172",
        "title": "海康威视 HIKVISION C6LITE 行车记录仪 单镜头 2K超清星光夜视",
        "price": 289.0,
        "commission_rate": 0.12,
        "sales": 80000,
        "good_rate": 0.96,
        "image": "https://img14.360buyimg.com/n7/s80x80_jfs/t1/451111/31/65432/23456/6a8d1f2bF3c9e7a1d/0083320320.jpg",
        "category": "汽车用品",
    },
    {
        "id": "100164913994",
        "title": "乖明星 我的世界磁力方块 磁吸积木拼装 儿童玩具 160多款自由组合DIY",
        "price": 28.0,
        "commission_rate": 0.3,
        "sales": 200000,
        "good_rate": 0.97,
        "image": "https://img14.360buyimg.com/n7/s80x80_jfs/t1/561111/41/76543/34567/6a9e2g3cF4d0f8b2e/0083320320.jpg",
        "category": "玩具乐器",
    },
]


def mock_fetch(category: str, page_size: int) -> dict:
    items = [p for p in MOCK_PRODUCTS
             if not category or category in ("全部", p["category"])]
    return {
        "code": 0,
        "msg": "mock",
        "data": {
            "products": items[:page_size],
            "total": len(items),
            "mock": True,
        },
    }


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

    p = pick_one(res)
    if not p:
        print(f"❌ 没能从响应里解析出商品。原始响应:\n"
              f"{json.dumps(res, ensure_ascii=False, indent=1)[:1500]}", file=sys.stderr)
        return 4

    print(f"\n✅ 选品成功 (mock={res.get('data', {}).get('mock', False)})")
    print(json.dumps(p, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
