"""乘车日期**可查性**判定 —— 12306 硬事实的**全项目唯一定义处**。

为什么需要单独一个模块
----------------------
12306 用「一行 HTML 错误页」**同时**表达两件毫不相干的事：

1. **真风控**（请求太频 / 被限流）—— 该降频、该等；
2. **日期不在可售范围**（已经过去 / 还没开售）—— 降频等到天荒地老也没用，只能换日期。

两者回的是**同一张页面**（``网络可能存在问题，请您重试一下！``，约 2 KB，HTTP 200，
``Content-Type`` 甚至不可靠）。原先代码把 HTML 一律当成风控，于是「任务日期填成了去年」
这种最简单的错误，在界面上被报成「🚨 风控告警」，使用者去降频、换账号、干等，
**全部无效** —— 而且每次重试都在给 12306 添一次注定被拒的请求。

真机实测（2026-09-29 20:37，**并排同一时刻、同一会话**，深圳→长沙南）::

    ======================  ==================  ==================
    乘车日期                 官方响应             事实
    ======================  ==================  ==================
    2026-10-03（今天+4）     JSON，84 个车次      —
    2026-10-12（今天+13）    JSON，82 个车次      —
    2026-10-13（今天+14）    JSON，75 个车次      **可查的最后一天**
    2026-10-14（今天+15）    HTML 错误页(2042B)   超出预售期
    2025-10-03（去年）       HTML 错误页(2042B)   已经过去
    2027-05-01（远期）       HTML 错误页(2042B)   超出预售期
    ======================  ==================  ==================

因此本模块对外只承诺一件事：**只有 ``[今天, 今天 + PRESALE_DAYS]`` 内的日期可查**。
复跑这张表的脚本：``python scripts/diagnose_left_ticket.py``。

⚠️ 不要在本模块之外再抄一份日期规则
------------------------------------
``PRESALE_DAYS`` 是 12306 会调整的政策（节假日会临时放长）。所以：

- 别在前端 / 界面里再写一遍「15 天」；
- 别用它去**阻止**发请求（政策放长时那会变成"明明有票却不查"）；
  它的用途是**给失败定性**，以及把「已过去」这种**确定性**错误提前拦掉。
  「已过去」永不可能变可查，所以拦掉是安全的；「未开售」会随时间变可查，所以只定性、不拦。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def today() -> date:
    """当前日期 —— **全项目唯一的「今天」来源**。

    为什么不让各处直接写 ``date.today()``：一旦「今天」散落在多个模块里，测试就没法冻结时钟，
    于是**任何**与日期有关的行为都只能靠真机试、或者在测试里写死年月日。
    而写死年月日的测试会**真的**在那一天过期，然后以「日期已过去」这种
    与它原本想验证的东西毫无关系的方式变红 —— 排查成本极高。

    测试里冻结时钟只需一行（``tests/conftest.py`` 就是这么做的）::

        monkeypatch.setattr(train_date, "today", lambda: date(2026, 9, 29))
    """
    return date.today()


#: 12306 预售期（**含当天**最多可查多少天之后）。
#: 实测 2026-09-29 可查至 2026-10-13，即「今天 + 14」。
#: 这是官方会调整的政策值，改动前请先用 ``scripts/diagnose_left_ticket.py`` 并排实测。
PRESALE_DAYS = 14

#: 判定结果
DATE_OK = "ok"                    # 在可售范围内
DATE_PAST = "past"                # 已经过去（确定不可查，永不会变）
DATE_NOT_ON_SALE = "not_on_sale"  # 还没开售（超出预售期，随时间会变可查）
DATE_BAD_FORMAT = "bad_format"    # 根本解析不出日期

DATE_FORMAT = "%Y-%m-%d"

#: 官方用来拒绝请求的那张页面的特征文字。
#: 风控与「日期不可查」共用它 —— 这正是需要本模块来区分的原因。
REJECT_PAGE_MARKER = "网络可能存在问题"


def parse(value: str | date | datetime | None) -> date | None:
    """尽量把输入解析成 ``date``；解析不出返回 ``None``（不抛异常）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return None
    for fmt in (DATE_FORMAT, "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def reference(now: date | None = None) -> date:
    """取「参照日」：显式传入的优先，否则用 :func:`today`（测试可冻结）。"""
    return now if now is not None else today()


def last_queryable_date(now: date | None = None) -> date:
    """12306 当前可查的最后一天。"""
    return reference(now) + timedelta(days=PRESALE_DAYS)


def classify(value: str | date | datetime | None, *, now: date | None = None) -> str:
    """判断日期能不能查，返回 ``DATE_*`` 之一。"""
    parsed = parse(value)
    if parsed is None:
        return DATE_BAD_FORMAT
    base = reference(now)
    if parsed < base:
        return DATE_PAST
    if parsed > base + timedelta(days=PRESALE_DAYS):
        return DATE_NOT_ON_SALE
    return DATE_OK


def queryable(value: str | date | datetime | None, *, now: date | None = None) -> bool:
    """该日期**现在**能不能查到余票。

    语义是「现在」：超出预售期的日期今天查不到，明天就可能查得到。
    所以拿它当"跳过该组合"的依据前，必须先想清楚这点 ——
    要确定性的判断请直接用 :func:`classify` 比对 ``DATE_PAST``。
    """
    return classify(value, now=now) == DATE_OK


def explain(value: str | date | datetime | None, *, now: date | None = None) -> str:
    """把判定结果写成人话（可直接给使用者看）。

    可查时返回空字符串 —— 调用方据此判断"一切正常，不用解释"。
    """
    base = reference(now)
    verdict = classify(value, now=base)
    last = last_queryable_date(base)
    if verdict == DATE_OK:
        return ""
    if verdict == DATE_PAST:
        return (
            f"乘车日期 {value} 已经过去（今天是 {base}），12306 不再提供该日期的余票，"
            f"请把任务里的乘车日期改到今天（{base}）或之后"
        )
    if verdict == DATE_NOT_ON_SALE:
        return (
            f"乘车日期 {value} 还没开售：12306 目前只能查 {base} ~ {last}"
            f"（预售 {PRESALE_DAYS + 1} 天，含当天），等开售后再查即可"
        )
    return f"乘车日期 {value} 格式非法，应为 2026-10-03 这样的 YYYY-MM-DD"


def is_reject_page(text: str) -> bool:
    """响应是不是 12306 那张「请求不被接受」的页面。

    同时覆盖风控与日期不可查两种成因 —— 想区分它们请用 :func:`classify`。
    """
    body = (text or "").lstrip("\ufeff").lstrip()
    if not body:
        return False
    return body.startswith("<") or REJECT_PAGE_MARKER in body
