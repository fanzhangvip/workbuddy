# -*- coding: utf-8 -*-
"""按角色的接口守卫（RBAC 最小集，架构 §6.1 的落地第一步）。

## 解决什么

改造前只有"登录 / 没登录"两态：**任何已认证的人都能发布规则、清理数据、给真实客户打标签**。
文章 B 的教训是权限必须在**真正执行的位置**由确定性代码判定 —— 这里就是那个位置。

## 为什么不用装饰器或中间件

用 FastAPI 的依赖（`dependencies=[Depends(require("admin"))]`）有三个好处：
  · 权限写在**接口声明处**，读代码时一眼看到，不会藏在函数体深处；
  · 未通过时自动返回 403，不需要每个函数自己处理；
  · 能出现在 OpenAPI 文档里（前端/调用方知道需要什么角色）。

## 与「工具网关」的分工

本模块管**接口级**权限（"这个人能不能调用这个功能"）；
`tools/` 那一层（ARCHITECTURE §7.3，尚未实现）管**动作级**权限
（"这个动作能不能自动执行、要不要人工签字"）。两者互补，不是二选一。
"""
from fastapi import HTTPException

import security


def require(*allowed: str):
    """返回一个 FastAPI 依赖：当前主体必须属于 ``allowed`` 之一，否则 403。

    - 鉴权关闭（`API_AUTH=off`）→ 直接放行（那是显式逃生阀，启动会打印警告）
    - 未认证 → 中间件已拦（401），走不到这里；若真走到，按"无权限"处理
    """
    allowed = allowed or security.ROLES

    def _dep():
        if not security.enabled():
            return security.LOCAL_ACTOR
        role = security.current_role()
        if role not in allowed:
            raise HTTPException(
                status_code=403,
                detail=(f"权限不足：本接口需要 {' / '.join(allowed)}，"
                        f"当前主体角色为 {role or '未认证'}。"
                        f"角色说明：{security.ROLE_DESC}"))
        return security.current_principal()

    return _dep
