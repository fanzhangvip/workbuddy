"""
数据文件管理：查看信息、预览、上传覆盖、删除。

管理的对象是唯一一份「当前使用」的数据文件 config.DATA_FILE（data/data.csv）。
上传时会校验必需字段，并把原始文件名等信息记录到 config.META_FILE。
"""

import io
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

import config
from core import loader


# ---------------- 元信息（记录来源文件） ----------------

def read_meta() -> dict:
    """读取来源文件信息，读取失败时返回空字典。"""
    if not config.META_FILE.exists():
        return {}
    try:
        return json.loads(config.META_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_meta(meta: dict):
    """写入来源文件信息。"""
    config.META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------- 工具 ----------------

def _size_text(size: int) -> str:
    """字节数转成易读文本。"""
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _json_safe(value):
    """把 numpy / NaN 等类型转成可 JSON 序列化的值。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):        # numpy 标量
        return value.item()
    return value


# ---------------- 查询 ----------------

def exists() -> bool:
    """当前是否存在数据文件。"""
    return config.DATA_FILE.exists()


def info() -> dict:
    """
    数据文件信息：文件名、大小、修改时间、行列数、字段完整性。

    对应管理后台顶部的信息卡与字段检查清单。
    """
    if not exists():
        return {
            "exists": False,
            "fileName": "",
            "size": 0,
            "sizeText": "-",
            "modifiedAt": "",
            "uploadedAt": None,
            "rows": 0,
            "cols": 0,
            "columns": [],
            "required": list(loader.REQUIRED_COLUMNS),
            "fields": [{"name": c, "present": False} for c in loader.REQUIRED_COLUMNS],
            "missing": list(loader.REQUIRED_COLUMNS),
            "valid": False,
        }

    stat = config.DATA_FILE.stat()
    meta = read_meta()
    columns = loader.file_columns()

    # 行数走 loader 缓存；文件内容有问题时退化为 0，不影响信息展示
    try:
        rows = len(loader.load_data())
    except Exception:
        rows = 0

    fields = [{"name": c, "present": c in columns} for c in loader.REQUIRED_COLUMNS]
    missing = [f["name"] for f in fields if not f["present"]]

    return {
        "exists": True,
        "fileName": meta.get("originalName") or config.DATA_FILE.name,
        "storedName": config.DATA_FILE.name,
        "size": stat.st_size,
        "sizeText": _size_text(stat.st_size),
        "modifiedAt": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "uploadedAt": meta.get("uploadedAt"),
        "rows": rows,
        "cols": len(columns),
        "columns": columns,
        "required": list(loader.REQUIRED_COLUMNS),
        "fields": fields,
        "missing": missing,
        "valid": len(missing) == 0,
    }


def preview(limit: int = 15) -> dict:
    """预览数据前若干行。"""
    if not exists():
        return {"columns": [], "rows": [], "total": 0}

    try:
        df = pd.read_csv(config.DATA_FILE, encoding="utf-8-sig")
    except Exception as exc:
        return {"columns": [], "rows": [], "total": 0, "error": str(exc)}

    head = df.head(limit)
    return {
        "columns": list(df.columns),
        "rows": [[_json_safe(v) for v in row] for row in head.values],
        "total": len(df),
        "shown": len(head),
    }


# ---------------- 上传 / 覆盖 ----------------

def _parse_upload(raw: bytes, ext: str) -> pd.DataFrame:
    """把上传的字节流解析成 DataFrame（CSV 自动尝试多种编码）。"""
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(io.BytesIO(raw))

    last_error = None
    for encoding in ("utf-8-sig", "gbk", "utf-8"):
        try:
            return pd.read_csv(io.BytesIO(raw), encoding=encoding)
        except Exception as exc:  # 换下一种编码再试
            last_error = exc
    raise ValueError(f"CSV 解析失败：{last_error}")


def save_upload(file_storage) -> dict:
    """
    校验并保存上传的表格文件，覆盖当前数据。

    返回 {"ok": bool, "msg": str, "missing": [...]}
    """
    filename = (file_storage.filename or "").strip()
    if not filename:
        return {"ok": False, "msg": "未选择文件"}

    ext = Path(filename).suffix.lower()
    if ext not in config.SUPPORTED_EXTS:
        return {"ok": False, "msg": f"不支持的文件类型 {ext}，仅支持 {' / '.join(config.SUPPORTED_EXTS)}"}

    raw = file_storage.stream.read()
    if not raw:
        return {"ok": False, "msg": "文件内容为空"}

    # 1) 先解析，失败直接返回，不动现有数据
    try:
        df = _parse_upload(raw, ext)
    except Exception as exc:
        return {"ok": False, "msg": f"文件解析失败：{exc}"}

    # 2) 字段完整性校验
    missing = [c for c in loader.REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return {
            "ok": False,
            "msg": "缺少必需字段：" + "、".join(missing),
            "missing": missing,
            "found": list(df.columns),
        }

    if df.empty:
        return {"ok": False, "msg": "文件中没有数据行"}

    # 3) 落盘：先留存原始上传件，再写标准 CSV
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        original_path = config.UPLOAD_DIR / f"{stamp}_{filename}"
        original_path.write_bytes(raw)

        df.to_csv(config.DATA_FILE, index=False, encoding="utf-8-sig")
    except PermissionError:
        return {"ok": False, "msg": "写入失败：data/data.csv 正被其他程序占用（如 Excel），请关闭后重试"}
    except Exception as exc:
        return {"ok": False, "msg": f"写入失败：{exc}"}

    write_meta({
        "originalName": filename,
        "uploadedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "originalSize": len(raw),
        "rows": len(df),
    })

    loader.clear_cache()   # 让大屏下次请求读到新数据
    return {"ok": True, "msg": f"上传成功，共 {len(df)} 条记录，已覆盖原数据"}


# ---------------- 删除 ----------------

def delete() -> dict:
    """删除当前数据文件（大屏将显示暂无数据）。"""
    if not exists():
        return {"ok": False, "msg": "当前没有数据文件可删除"}

    try:
        config.DATA_FILE.unlink()
    except PermissionError:
        return {"ok": False, "msg": "删除失败：文件正被其他程序占用（如 Excel），请关闭后重试"}
    except Exception as exc:
        return {"ok": False, "msg": f"删除失败：{exc}"}

    if config.META_FILE.exists():
        config.META_FILE.unlink()

    loader.clear_cache()
    return {"ok": True, "msg": "数据文件已删除，大屏将显示暂无数据"}
