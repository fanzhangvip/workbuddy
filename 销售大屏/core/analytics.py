"""
销售数据聚合计算。

所有函数都返回可直接被 jsonify 序列化的结构（dict / list），
字段名统一用英文，中文标签由前端负责。

包含大屏所需的 9 块数据：
KPI 核心指标 / 月度趋势 / 地区占比 / 产品排行 / 渠道占比
/ 利润进度 / 地区热榜 / 地图分布 / 销售明细
"""

from datetime import datetime

import config
from core import loader
from core.loader import load_data


def _round(value, digits: int = 2) -> float:
    """统一四舍五入，避免浮点误差带出长小数。"""
    return round(float(value), digits)


# ---------------- 顶部核心指标 ----------------

def kpi() -> dict:
    """核心指标卡：销售额 / 利润 / 销量 / 订单数 / 毛利率 / 客单价。"""
    df = load_data()
    revenue = df["销售额"].sum()
    profit = df["利润"].sum()

    return {
        "revenue": _round(revenue),
        "profit": _round(profit),
        "quantity": int(df["销量"].sum()),
        "orders": int(len(df)),
        # 毛利率 = 利润 / 销售额
        "margin": _round(profit / revenue * 100 if revenue else 0, 1),
        # 客单价 = 销售额 / 订单数
        "avgPrice": _round(revenue / len(df) if len(df) else 0),
    }


# ---------------- 中间：月度趋势 ----------------

def monthly_trend() -> dict:
    """月度趋势：每月销售额与利润。"""
    df = load_data()
    grouped = (
        df.groupby("月份", as_index=False)
        .agg(销售额=("销售额", "sum"), 利润=("利润", "sum"))
        .sort_values("月份")
    )
    return {
        "months": grouped["月份"].tolist(),
        "revenue": [_round(v) for v in grouped["销售额"]],
        "profit": [_round(v) for v in grouped["利润"]],
    }


# ---------------- 左栏：地区占比 / 产品排行 ----------------

def province_share(top: int = 8) -> list:
    """地区占比（按销售额），超出 top 的合并为「其他」。"""
    df = load_data()
    series = df.groupby("省份")["销售额"].sum().sort_values(ascending=False)

    data = [{"name": name, "value": _round(v)} for name, v in series.head(top).items()]
    rest = series.iloc[top:].sum()
    if rest > 0:
        data.append({"name": "其他", "value": _round(rest)})
    return data


def product_rank(top: int = None) -> list:
    """产品排行（按销售额）取前 N。"""
    df = load_data()
    series = (
        df.groupby("产品")["销售额"].sum()
        .sort_values(ascending=False)
        .head(top or config.TOP_N)
    )
    return [{"name": name, "value": _round(v)} for name, v in series.items()]


# ---------------- 右栏：渠道占比 / 利润进度 / 地区热榜 ----------------

def channel_share() -> list:
    """渠道占比（按销售额）。"""
    df = load_data()
    series = df.groupby("渠道")["销售额"].sum().sort_values(ascending=False)
    return [{"name": name, "value": _round(v)} for name, v in series.items()]


def profit_progress(top: int = 6) -> list:
    """
    利润进度：销售额 Top N 产品的利润与利润率。

    percent 为该产品销售额占榜首的比例（0~100），前端据此画进度条长度。
    """
    df = load_data()
    grouped = (
        df.groupby("产品", as_index=False)
        .agg(销售额=("销售额", "sum"), 利润=("利润", "sum"))
        .sort_values("销售额", ascending=False)
        .head(top)
    )
    max_revenue = grouped["销售额"].max() or 1  # 防除零

    result = []
    for _, row in grouped.iterrows():
        revenue = float(row["销售额"])
        profit = float(row["利润"])
        result.append({
            "name": row["产品"],
            "revenue": _round(revenue),
            "profit": _round(profit),
            "margin": _round(profit / revenue * 100 if revenue else 0, 1),
            "percent": _round(revenue / max_revenue * 100, 1),
        })
    return result


def region_hot(top: int = 8) -> list:
    """地区热榜（按销售额）取前 N。"""
    df = load_data()
    series = df.groupby("省份")["销售额"].sum().sort_values(ascending=False).head(top)
    return [{"name": name, "value": _round(v)} for name, v in series.items()]


# ---------------- 中间：地图分布 ----------------

def map_data() -> list:
    """地图分布：每个省份的销售额（省份用短名，前端映射到地图全名）。"""
    df = load_data()
    series = df.groupby("省份")["销售额"].sum()
    return [{"name": name, "value": _round(v)} for name, v in series.items()]


# ---------------- 左栏：销售明细 ----------------

def detail(limit: int = 40) -> list:
    """销售明细：按日期倒序取最近若干条。"""
    df = load_data().sort_values("日期", ascending=False).head(limit)
    return [
        {
            "date": row["日期"].strftime("%m-%d"),
            "province": row["省份"],
            "product": row["产品"],
            "channel": row["渠道"],
            "quantity": int(row["销量"]),
            "revenue": _round(row["销售额"]),
        }
        for _, row in df.iterrows()
    ]


# ---------------- 汇总 ----------------

def empty_payload(reason: str = "") -> dict:
    """
    无数据时的统一返回结构。

    保持与正常返回完全相同的字段形状，前端无需分支处理，
    只需看 empty 标记决定是否显示「暂无数据」。
    """
    return {
        "empty": True,
        "reason": reason,
        "kpi": {"revenue": 0, "profit": 0, "quantity": 0,
                "orders": 0, "margin": 0, "avgPrice": 0},
        "trend": {"months": [], "revenue": [], "profit": []},
        "provinceShare": [],
        "productRank": [],
        "channelShare": [],
        "profitProgress": [],
        "regionHot": [],
        "map": [],
        "detail": [],
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def dashboard() -> dict:
    """
    一次性返回大屏所需的全部数据，前端只需一个请求。

    没有数据文件、或文件存在但内容不可用（缺列 / 格式错误 / 被占用）时，
    返回空结构而不是抛异常，让大屏能优雅显示「暂无数据」。
    """
    if not loader.data_exists():
        return empty_payload("尚未上传数据文件")

    try:
        return {
            "empty": False,
            "reason": "",
            "kpi": kpi(),
            "trend": monthly_trend(),
            "provinceShare": province_share(),
            "productRank": product_rank(),
            "channelShare": channel_share(),
            "profitProgress": profit_progress(),
            "regionHot": region_hot(),
            "map": map_data(),
            "detail": detail(),
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:  # noqa: BLE001  数据不可用时降级为空态，避免大屏白屏
        return empty_payload(f"数据不可用：{exc}")
