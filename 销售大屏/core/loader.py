"""
数据加载：把 data/data.csv 读成规范的 DataFrame。

职责单一 —— 只负责「读文件 + 类型转换 + 校验 + 缓存」，
不做任何业务聚合（聚合在 core/analytics.py）。
"""

import pandas as pd

import config

# 数据表必需的列（与 data/data.csv 的表头一致）
REQUIRED_COLUMNS = ["日期", "省份", "产品", "渠道", "销量", "销售额", "利润"]

# 数值列
NUMERIC_COLUMNS = ["销量", "销售额", "利润"]

# 简易缓存：文件没变动就不重复解析
_cache = {"df": None, "mtime": None}


def load_data(force: bool = False) -> pd.DataFrame:
    """
    读取销售数据表。

    :param force: True 时忽略缓存，强制重新读取
    :return: 已完成类型转换的 DataFrame
    """
    path = config.DATA_FILE
    if not path.exists():
        raise FileNotFoundError(f"未找到数据文件：{path}，请先运行 python -m core.mock_data 生成")

    mtime = path.stat().st_mtime
    if not force and _cache["df"] is not None and _cache["mtime"] == mtime:
        return _cache["df"]

    # utf-8-sig：兼容带 BOM 的文件（Excel 另存为 CSV 常见）
    df = pd.read_csv(path, encoding="utf-8-sig")

    # 校验列是否齐全，缺失时给出明确提示
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"数据缺少必需列：{missing}；实际列：{list(df.columns)}")

    # 类型转换
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 丢弃日期或金额无效的行
    df = df.dropna(subset=["日期", "销售额"]).copy()

    # 派生字段：月份（YYYY-MM），供月度趋势聚合使用
    df["月份"] = df["日期"].dt.to_period("M").astype(str)

    _cache["df"] = df
    _cache["mtime"] = mtime
    return df


def data_exists() -> bool:
    """当前是否已有数据文件。"""
    return config.DATA_FILE.exists()


def file_columns() -> list:
    """
    只读表头，返回文件里的列名。

    比 load_data() 轻量（不解析全部数据），用于「字段完整性」检查，
    即使文件内容有问题也能拿到列名。
    """
    if not data_exists():
        return []
    try:
        return list(pd.read_csv(config.DATA_FILE, encoding="utf-8-sig", nrows=0).columns)
    except Exception:
        return []


def clear_cache():
    """清空缓存，下次 load_data() 会重新解析文件。"""
    _cache["df"] = None
    _cache["mtime"] = None
