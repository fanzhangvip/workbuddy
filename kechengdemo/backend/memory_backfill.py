# -*- coding: utf-8 -*-
"""一次性迁移：把已落库的 `profile_struct` 回填成 `customer_memory`。

用法（默认 **dry-run**，只报数不写库）::

    python -m backend.memory_backfill            # 看会处理多少客户
    python -m backend.memory_backfill --apply    # 真正写入

为什么单独一个脚本：这是**数据迁移**，不是运行时逻辑。放在服务启动路径里会变成
"每次重启都跑一次迁移"，一旦有 bug 就是静默改数据；命令行 + dry-run 才有可审性。

只处理"还没有任何 active 记忆的客户"：重复执行不会覆盖人工改过的值。
"""
import argparse
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import db            # noqa: E402
import memory_repo   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把 profile_struct 回填成客户记忆")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只 dry-run）")
    ap.add_argument("--ttl", type=int, default=0, help="有效期天数（默认用 memory_ttl_days）")
    args = ap.parse_args()
    db.init_db()
    res = memory_repo.backfill(dry_run=not args.apply, ttl=args.ttl or None)
    print(f"[memory] 待处理客户 {res['customers']} 个；写入 {res['written']} 条"
          f"；dry_run={res['dry_run']}")
    if res["dry_run"] and res["customers"]:
        print("[memory] 确认无误后重跑：python -m backend.memory_backfill --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
