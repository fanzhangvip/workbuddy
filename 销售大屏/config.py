"""
全局配置：目录路径与运行参数。

对接真实数据时，通常只需要修改：
  1) 本文件顶部的路径配置（一般不用改）
  2) core/schema.py 里的列名映射表（把你的表头对到标准字段）
"""

from pathlib import Path
import os

# ---------- 路径 ----------
BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"           # 用户直接放进来的原始数据
UPLOAD_DIR = DATA_DIR / "uploads"    # 网页上传的数据
MOCK_DIR = DATA_DIR / "mock"         # 自动生成的模拟数据
MOCK_FILE = MOCK_DIR / "sales_mock.csv"

DATA_FILE = DATA_DIR / "data.csv"    # 当前使用的销售数据表
META_FILE = DATA_DIR / ".meta.json"  # 记录数据来源文件信息（原始文件名等）

# ---------- 支持的表格类型 ----------
SUPPORTED_EXTS = (".xlsx", ".xls", ".csv")

# ---------- Flask 服务 ----------
HOST = "127.0.0.1"
PORT = 5000
DEBUG = True

# ---------- 业务默认值 ----------
TOP_N = 10                # Top 商品 / 排行榜默认条数
DETAIL_PAGE_SIZE = 20     # 明细表默认每页条数

# 确保必要目录存在（导入 config 即生效，避免各处重复判断）
for _d in (DATA_DIR, RAW_DIR, UPLOAD_DIR, MOCK_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# ---------- AI 数据分析师（DeepSeek） ----------

def _load_env(path: Path) -> None:
    """
    极简 .env 读取：每行 KEY=VALUE，支持 # 注释。

    刻意不引入 python-dotenv，保持依赖精简。
    已存在的环境变量优先，不会被文件覆盖。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE_DIR / ".env")

# 模型配置（密钥放在 .env，不要写进代码）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# 单次回答的最大 token 数（该模型带推理，需留足预算）
AI_MAX_TOKENS = 4096
# agent 最多思考几步，防止工具调用死循环
AI_RECURSION_LIMIT = 12
