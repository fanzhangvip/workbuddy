# -*- coding: utf-8 -*-
"""列表类接口的公共骨架 —— 目前只有「分页」这一件。

## 为什么要有它

「条件 → LIMIT/OFFSET → count → 出参字典」这 5 步，改造前在 3 个文件里各写了一遍：

    api_scoring.assessments   ·  api_eval.list_cases  ·  api_review.list_reviews

连 `(max(1, page) - 1) * page_size` 这个边界写法都逐字相同，出参也逐字相同。
后果是「给分页加个单页上限」要改 3 处，漏一处就是**一个接口能被 `page_size=10 万`
拖垮**（分页接口最典型的自伤）。

**只收这最后一步**：过滤条件仍由调用方自建 —— 各表的列名与语义不同
（`a.is_current` / `r.status` / `prefilled`），硬抽成一个 DSL 只会更难读。
"""
import db

#: 单页上限。分页接口的护栏：`page_size` 是用户可控入参，不设上限等于
#: 允许一次请求把整表（78 万会话消息的量级）拉进内存。
MAX_PAGE_SIZE = 200

#: 单页默认值（与前端三处列表的默认一致）。
DEFAULT_PAGE_SIZE = 20


def paginate(select_sql: str, count_sql: str, where: str, params: dict, *,
             order_by: str = "", page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
             max_page_size: int = MAX_PAGE_SIZE) -> dict:
    """执行「取一页 + 取总数」，返回 ``{"items","total","page","page_size"}``。

    ``select_sql`` / ``count_sql`` 里的表别名要与 ``where`` 一致
    （两处都带 `a.` 前缀这种），``where`` 传不带 ``WHERE`` 关键字的条件串。

    ``params`` 用 ``%(name)s`` 占位（psycopg 的字典参数），本函数只往里加
    `limit` / `offset`，并保证 count 查询**不带**这两个键 —— 否则 psycopg
    会因「SQL 里没用到的参数」报错。

    ``page`` / ``page_size`` 都会被夹到合法区间：越界的入参不报错、按边界处理
    （列表接口因为一个 page=0 就 500，是纯粹的可用性自伤）。
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), int(max_page_size)))
    p = dict(params or {})
    p["limit"], p["offset"] = page_size, (page - 1) * page_size

    sql = f"{select_sql.strip()} WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    sql += " LIMIT %(limit)s OFFSET %(offset)s"
    items = db.query(sql, p)

    total = db.query_one(f"{count_sql.strip()} WHERE {where}",
                         {k: v for k, v in p.items() if k not in ("limit", "offset")})
    return {"items": items, "total": (total or {}).get("n", 0),
            "page": page, "page_size": page_size}
