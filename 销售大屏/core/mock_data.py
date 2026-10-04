"""
模拟销售数据生成器（美妆行业）。

用途：真实数据到位前，先生成一份结构完整的样例数据，让大屏能跑起来。
生成的字段：日期、省份、产品、渠道、销量、销售额、利润

运行方式（在项目根目录）：
    python -m core.mock_data
"""

import random
from datetime import date, timedelta

import pandas as pd

import config

# 随机种子：固定后每次生成结果一致，方便调试复现
RANDOM_SEED = 42

# 数据行数
ROW_COUNT = 300

# 日期范围：跨一年，方便后续做同比/环比
DATE_START = date(2025, 9, 1)
DATE_END = date(2026, 9, 28)

# 全部 34 个省级行政区（短名，与地图数据命名保持一致）
PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古",
    "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东",
    "河南", "湖北", "湖南", "广东", "广西", "海南",
    "重庆", "四川", "贵州", "云南", "西藏",
    "陕西", "甘肃", "青海", "宁夏", "新疆",
    "台湾", "香港", "澳门",
]

# 省份权重：经济发达地区销量更高，更贴近真实分布
PROVINCE_WEIGHTS = {
    "广东": 10, "浙江": 8, "江苏": 8, "上海": 7, "北京": 7, "山东": 6,
    "四川": 5, "福建": 5, "湖北": 4, "湖南": 4, "河南": 4, "河北": 3,
    "安徽": 3, "陕西": 3, "辽宁": 3, "重庆": 3, "江西": 2, "广西": 2,
    "云南": 2, "天津": 2, "山西": 2, "贵州": 2, "黑龙江": 2, "吉林": 2,
    "内蒙古": 1, "新疆": 1, "甘肃": 1, "海南": 1, "宁夏": 1, "青海": 1,
    "西藏": 1, "台湾": 2, "香港": 2, "澳门": 1,
}

# 美妆产品 => (最低单价, 最高单价, 基础毛利率)
PRODUCTS = {
    "口红": (89, 320, 0.42),
    "唇釉": (79, 260, 0.40),
    "粉底液": (129, 450, 0.35),
    "气垫BB": (139, 480, 0.33),
    "遮瑕膏": (69, 220, 0.38),
    "眼影盘": (99, 360, 0.40),
    "睫毛膏": (59, 190, 0.41),
    "眼线笔": (39, 130, 0.43),
    "眉笔": (29, 110, 0.45),
    "腮红": (69, 230, 0.39),
    "高光": (89, 280, 0.37),
    "散粉": (79, 240, 0.36),
    "卸妆水": (49, 150, 0.30),
    "洁面乳": (39, 160, 0.28),
    "爽肤水": (69, 260, 0.32),
    "精华液": (159, 680, 0.45),
    "面霜": (129, 590, 0.44),
    "乳液": (89, 320, 0.34),
    "眼霜": (119, 450, 0.46),
    "防晒霜": (79, 290, 0.31),
    "面膜": (39, 180, 0.29),
    "香水": (199, 880, 0.48),
    "指甲油": (25, 90, 0.44),
    "唇膏": (59, 200, 0.41),
    "化妆刷": (45, 300, 0.35),
}

# 销售渠道
CHANNELS = [
    "天猫旗舰店", "京东自营", "抖音直播间", "拼多多", "唯品会",
    "小红书商城", "线下专柜", "屈臣氏", "微信小程序", "亚马逊海外",
]


def _random_date() -> date:
    """在 DATE_START ~ DATE_END 之间随机取一天。"""
    span = (DATE_END - DATE_START).days
    return DATE_START + timedelta(days=random.randint(0, span))


def generate_rows(n: int = ROW_COUNT) -> list[dict]:
    """生成 n 条销售记录。"""
    province_pool = list(PROVINCE_WEIGHTS.keys())
    province_weights = list(PROVINCE_WEIGHTS.values())
    product_names = list(PRODUCTS.keys())

    rows = []
    for _ in range(n):
        product = random.choice(product_names)
        price_lo, price_hi, base_margin = PRODUCTS[product]

        province = random.choices(province_pool, weights=province_weights, k=1)[0]
        channel = random.choice(CHANNELS)

        # 销量：1~50 之间，小单更多，用平方让分布偏向低值
        quantity = random.randint(1, 50)

        # 单价在区间内浮动，按销量给一点折扣（量大价低）
        unit_price = random.uniform(price_lo, price_hi)
        if quantity > 20:
            unit_price *= 0.92

        revenue = round(quantity * unit_price, 2)

        # 毛利率在基础值上下浮动 ±5 个百分点，保证 利润 < 销售额
        margin = min(max(base_margin + random.uniform(-0.05, 0.05), 0.05), 0.75)
        profit = round(revenue * margin, 2)

        rows.append({
            "日期": _random_date().isoformat(),
            "省份": province,
            "产品": product,
            "渠道": channel,
            "销量": quantity,
            "销售额": revenue,
            "利润": profit,
        })

    return rows


def main():
    """生成模拟数据并写入 data/data.csv。"""
    random.seed(RANDOM_SEED)  # 固定种子，结果可复现

    rows = generate_rows()
    df = pd.DataFrame(rows)

    # 按日期排序，看起来更像真实流水
    df = df.sort_values("日期").reset_index(drop=True)

    # utf-8-sig：带 BOM，用 Excel 打开不会中文乱码
    df.to_csv(config.DATA_FILE, index=False, encoding="utf-8-sig")

    print(f"已生成 {len(df)} 条模拟销售数据 -> {config.DATA_FILE}")
    print(f"日期范围: {df['日期'].min()} ~ {df['日期'].max()}")
    print(f"覆盖省份: {df['省份'].nunique()} 个 | 产品: {df['产品'].nunique()} 种 | 渠道: {df['渠道'].nunique()} 个")


if __name__ == "__main__":
    main()
