"""
AI 数据分析师使用的数据查询工具。

安全设计原则
------------
1. 只接受**结构化参数**（维度 / 指标 / 筛选条件 / 分析方式），
   不接受任何代码字符串，从根上杜绝任意代码执行。
2. 所有计算都走本文件里固定的 Pandas 聚合逻辑，模型无法绕过。
3. **只读**：本文件不含任何写文件、改数据的操作，AI 无法修改 CSV 或大屏。
4. 查询无结果时如实返回「无数据」，不编造。

工具通过 artifact 回传「本次实际执行的 Pandas 表达式」，
供前端展示"实际使用的 Pandas 工具"，结论来源可核对。
"""

from langchain_core.tools import tool
import pandas as pd

from core.loader import load_data

# ---- 允许的取值范围（模型只能在这几个值里选） ----
ANALYSES = {
    "summary": "总体统计：合计、均值、最大、最小、记录数",
    "rank": "按维度排名（降序，含名次）",
    "compare": "按维度对比（含占比、与平均水平对比）",
    "trend": "按时间趋势（升序，含环比变化）",
    "options": "列出某个维度下所有可选值（不知道有哪些省份/产品/渠道时先查这个）",
}

METRICS = ["销量", "销售额", "利润", "利润率"]

DIMENSIONS = ["省份", "产品", "渠道", "日期", "月份", "季度", "年度"]

# 时间维度的排序优先级（趋势分析用）
TIME_DIMENSIONS = ["日期", "月份", "季度", "年度"]


# ---------------- 内部工具 ----------------

def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """补上时间派生列，供按日期/季度/年度聚合使用。"""
    df = df.copy()
    df["日"] = df["日期"].dt.strftime("%Y-%m-%d")
    df["季度"] = df["日期"].dt.year.astype(str) + "Q" + df["日期"].dt.quarter.astype(str)
    df["年度"] = df["日期"].dt.year.astype(str)
    return df


def _split(text: str) -> list:
    """把「广东,浙江」这类逗号分隔文本拆成列表，兼容中文逗号。"""
    if not text:
        return []
    return [item.strip() for item in text.replace("，", ",").split(",") if item.strip()]


def _apply_filters(df: pd.DataFrame, start_date: str, end_date: str,
                   provinces: str, products: str, channels: str):
    """
    按日期与文本条件筛选。

    :return: (筛选后数据, 条件说明列表, 与筛选等价的 pandas 表达式)
    """
    notes = []
    conditions = []

    if start_date:
        df = df[df["日期"] >= pd.to_datetime(start_date, errors="coerce")]
        notes.append(f"起始日期 {start_date}")
        conditions.append(f"df['日期'] >= '{start_date}'")
    if end_date:
        df = df[df["日期"] <= pd.to_datetime(end_date, errors="coerce")]
        notes.append(f"结束日期 {end_date}")
        conditions.append(f"df['日期'] <= '{end_date}'")

    # 文本条件用「包含」匹配，这样「广东」也能命中「广东省」这类写法
    for column, text in (("省份", provinces), ("产品", products), ("渠道", channels)):
        values = _split(text)
        if values:
            mask = pd.Series(False, index=df.index)
            for value in values:
                mask |= df[column].astype(str).str.contains(value, na=False, regex=False)
            df = df[mask]
            notes.append(f"{column} ∈ {values}")
            joined = "|".join(values)
            conditions.append(f"df['{column}'].str.contains('{joined}')")

    # 各条件之间是「与」关系，拼成一个等价表达式用于展示
    expr = "df[" + " & ".join(conditions) + "]" if conditions else "df"
    return df, notes, expr


def _aggregate(df: pd.DataFrame, keys) -> pd.DataFrame:
    """按指定维度聚合，并计算利润率。"""
    grouped = df.groupby(keys, as_index=False).agg(
        销量=("销量", "sum"),
        销售额=("销售额", "sum"),
        利润=("利润", "sum"),
    )
    # 利润率 = 利润 / 销售额，避免除零
    grouped["利润率"] = [
        round(p / r * 100, 2) if r else 0.0
        for p, r in zip(grouped["利润"], grouped["销售额"])
    ]
    return grouped


def _dimension_column(dimension: str) -> str:
    """维度名转成 DataFrame 里的列名（日期 -> 日）。"""
    return "日" if dimension == "日期" else dimension


def _fmt(value) -> str:
    """数字格式化：金额保留两位并加千分位。"""
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def _agg_expr(base: str, dimension: str) -> str:
    """生成聚合部分的 pandas 表达式片段（与实际计算一一对应）。"""
    column = _dimension_column(dimension)
    return (f"{base}.groupby('{column}').agg({{'销量':'sum','销售额':'sum','利润':'sum'}})")


# ---------------- 工具本体 ----------------

@tool(response_format="content_and_artifact")
def query_sales_data(
    analysis: str,
    metric: str = "销售额",
    dimension: str = "",
    start_date: str = "",
    end_date: str = "",
    provinces: str = "",
    products: str = "",
    channels: str = "",
    top_n: int = 10,
) -> tuple:
    """查询销售数据并做统计、排名、对比或趋势分析。

    所有数字都来自真实的销售数据表计算，禁止凭记忆编造。
    本工具是只读的，不会修改任何数据。

    Args:
        analysis: 分析类型，必须是以下之一：
            summary - 总体统计（合计/均值/最大/最小/记录数），不需要 dimension
            rank    - 按维度排名，降序取前 top_n，需要 dimension
            compare - 按维度对比，含占比与相对平均水平的偏离，需要 dimension
            trend   - 按时间趋势，升序并给出环比变化，dimension 用 月份/日期/季度/年度
            options - 列出某维度所有可选值（不确定有哪些省份/产品/渠道时先用它）
        metric: 指标，取值 销量 / 销售额 / 利润 / 利润率（默认 销售额）
        dimension: 聚合维度，取值 省份 / 产品 / 渠道 / 日期 / 月份 / 季度 / 年度
        start_date: 起始日期，格式 YYYY-MM-DD，可留空
        end_date: 结束日期，格式 YYYY-MM-DD，可留空
        provinces: 省份筛选，多个用逗号分隔，如「广东,浙江」，可留空表示全部
        products: 产品筛选，多个用逗号分隔，可留空表示全部
        channels: 渠道筛选，多个用逗号分隔，可留空表示全部
        top_n: 排名/对比返回的条数，默认 10，最大 50

    Returns:
        (给模型看的文本结果, 给前端展示的元信息)
    """

    def fail(message: str):
        """统一的失败返回。"""
        return message, {"pandas": "", "args": {}, "ok": False}

    # ---- 参数校验：只允许既定取值 ----
    analysis = (analysis or "").strip().lower()
    if analysis not in ANALYSES:
        return fail(f"参数错误：analysis 只能是 {list(ANALYSES)} 之一，收到「{analysis}」。")
    if metric not in METRICS:
        return fail(f"参数错误：metric 只能是 {METRICS} 之一，收到「{metric}」。")
    if dimension and dimension not in DIMENSIONS:
        return fail(f"参数错误：dimension 只能是 {DIMENSIONS} 之一，收到「{dimension}」。")
    if analysis in ("rank", "compare") and not dimension:
        return fail("参数错误：rank / compare 分析必须指定 dimension。")
    if analysis == "trend" and dimension and dimension not in TIME_DIMENSIONS:
        return fail(f"参数错误：trend 的 dimension 必须是 {TIME_DIMENSIONS} 之一；不填则默认按月。")
    if analysis == "options" and not dimension:
        return fail("参数错误：options 分析必须指定 dimension。")

    top_n = max(1, min(int(top_n or 10), 50))

    # ---- 取数：没有数据文件时明确拒绝分析 ----
    try:
        df = _prepare(load_data())
    except FileNotFoundError:
        return fail("【无数据】当前还没有上传销售数据文件，无法进行任何数据分析。"
                    "请先在数据管理后台上传包含「日期、省份、产品、渠道、销量、销售额、利润」的文件。")
    except Exception as exc:
        return fail(f"【无法分析】数据文件读取失败：{exc}")

    if df.empty:
        return fail("【无数据】数据文件存在但没有有效记录，无法分析。")

    total_rows = len(df)
    df, filter_notes, base_expr = _apply_filters(
        df, start_date, end_date, provinces, products, channels
    )
    scope = "；".join(filter_notes) if filter_notes else "无筛选（全量数据）"

    if df.empty:
        return (f"【无数据】筛选条件（{scope}）下没有任何记录。"
                f"数据表共 {total_rows} 行。请放宽或修改条件；"
                f"如果不确定有哪些省份/产品/渠道，请先用 analysis=options 查询。",
                {"pandas": base_expr + "  # 结果为空", "args": {}, "ok": False})

    # ---- options：列出可选值 ----
    if analysis == "options":
        column = dimension
        counts = df[column].astype(str).value_counts()
        items = "、".join(f"{name}({count}条)" for name, count in counts.items())
        return (f"【可选值】维度={column}，共 {len(counts)} 个取值\n{items}",
                {"pandas": f"{base_expr}['{column}'].value_counts()",
                 "args": {"analysis": analysis, "dimension": dimension}, "ok": True})

    # ---- summary：总体统计 ----
    if analysis == "summary":
        revenue = float(df["销售额"].sum())
        margin = round(float(df["利润"].sum()) / revenue * 100, 2) if revenue else 0
        lines = [
            f"【总体统计】范围：{scope}",
            f"记录数：{len(df):,} 行",
            f"日期范围：{df['日期'].min():%Y-%m-%d} ~ {df['日期'].max():%Y-%m-%d}",
            "",
            f"销量合计：{int(df['销量'].sum()):,} 件",
            f"销售额合计：{_fmt(float(df['销售额'].sum()))} 元",
            f"利润合计：{_fmt(float(df['利润'].sum()))} 元",
            f"整体利润率：{margin} %",
            "",
            f"{metric} 均值：{_fmt(round(float(df[metric].mean()), 2))}",
            f"{metric} 最大：{_fmt(round(float(df[metric].max()), 2))}",
            f"{metric} 最小：{_fmt(round(float(df[metric].min()), 2))}",
        ]
        return ("\n".join(lines),
                {"pandas": f"{base_expr}[['销量','销售额','利润']].agg(['sum','mean','max','min'])",
                 "args": {"analysis": analysis, "metric": metric,
                          "start_date": start_date, "end_date": end_date,
                          "provinces": provinces, "products": products, "channels": channels},
                 "ok": True})

    # ---- 需要按维度聚合的分析 ----
    if analysis == "trend":
        dimension = dimension or "月份"
    column = _dimension_column(dimension)

    table = _aggregate(df, column)
    agg_expr = _agg_expr(base_expr, dimension)

    if analysis == "trend":
        table = table.sort_values(column, ascending=True)
        table["环比%"] = table[metric].pct_change().mul(100).round(2)
        # 首期没有环比，用文字占位更清楚
        table["环比%"] = table["环比%"].apply(lambda v: "—" if pd.isna(v) else f"{v:+.2f}")
        head = f"【趋势】{dimension}维度的 {metric}（升序，含环比）｜范围：{scope}"
        pandas_expr = f"{agg_expr}.sort_values('{column}')['{metric}'].pct_change()"
    elif analysis == "rank":
        table = table.sort_values(metric, ascending=False).head(top_n)
        table.insert(0, "名次", range(1, len(table) + 1))
        head = f"【排名】按 {dimension} 的 {metric}（前 {len(table)} 名）｜范围：{scope}"
        pandas_expr = f"{agg_expr}.sort_values('{metric}', ascending=False).head({top_n})"
    else:  # compare
        table = table.sort_values(metric, ascending=False).head(top_n)
        total = table[metric].sum()
        average = table[metric].mean()
        table["占比%"] = [round(v / total * 100, 2) if total else 0 for v in table[metric]]
        table["vs均值"] = [
            f"{(v / average - 1) * 100:+.1f}%" if average else "—"
            for v in table[metric]
        ]
        head = f"【对比】按 {dimension} 的 {metric}（前 {len(table)} 项）｜范围：{scope}"
        pandas_expr = (f"{agg_expr}.sort_values('{metric}', ascending=False).head({top_n})"
                       f"  # 另算占比 {metric}/{metric}.sum() 与 vs均值")

    # 只保留常用列，避免输出过宽
    columns = [c for c in ["名次", column, "销量", "销售额", "利润", "利润率", "占比%", "vs均值", "环比%"]
               if c in table.columns]
    body = table[columns].to_string(index=False)

    text = (f"{head}\n筛选后数据共 {df[column].nunique()} 个「{dimension}」取值，"
            f"下表展示 {len(table)} 项\n\n{body}")

    return (text,
            {"pandas": pandas_expr,
             "args": {"analysis": analysis, "metric": metric, "dimension": dimension,
                      "start_date": start_date, "end_date": end_date,
                      "provinces": provinces, "products": products, "channels": channels,
                      "top_n": top_n},
             "ok": True})


# 供 agent 使用的工具列表
TOOLS = [query_sales_data]
