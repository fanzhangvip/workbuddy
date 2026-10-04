"""人证核验：状态语义 + 「怎么核验」的指引（**单一来源**）。

为什么值得单独一个模块
----------------------
「人证核验」这件事在代码里同时被四处用到：账号页的显示、候补引擎的前置判断、
通知正文、诊断脚本。如果各写一份，最容易出现的后果是
**同一个账号在 A 处显示「无法判定」、在 B 处被判成「未核验」** ——
而后者会直接拦掉一个本来可以用的账号。

官方口径（www.12306.cn 候补购票操作说明）
----------------------------------------
「为了保证候补需求的真实性，在申请该项服务前，用户需进行**人证一致性核验**。
目前已通过人证一致性核验的用户及激活的『铁路畅行』会员，可自动获得该项服务。
用户可通过铁路 12306 App 个人中心的人证核验入口按引导完成验证。」

所以核验是**在官方 App 里用摄像头做人脸比对**的事，第三方程序
既做不了、也不该代做（必须本人）。我们能做的是三件事：

1. **如实检测**：拿得到明确字段就给结论；拿不到就说「无法判定」，**绝不猜**；
2. **不误拦**：只有「明确未核验」才拦；「判不出来」要放行，让官方去判；
3. **给出路**：把「去 12306 App 哪里点」写清楚，并在通知里直接带上。
"""

from __future__ import annotations

from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# 四种状态
# --------------------------------------------------------------------------- ##: 拿到明确字段且通过
STATE_VERIFIED = "verified"
#: 拿到明确字段且未通过 —— **这才是真的会被候补拦住的状态**
STATE_UNVERIFIED = "unverified"
#: 接口没有返回核验字段（真机实测确实如此）→ 判不出来，不猜
STATE_UNKNOWN = "unknown"
#: 还没登录成功，压根没检测过
STATE_UNCHECKED = "unchecked"

LABEL: dict[str, str] = {
    STATE_VERIFIED: "已核验",
    STATE_UNVERIFIED: "未核验",
    STATE_UNKNOWN: "无法判定",
    STATE_UNCHECKED: "未检测",
}

#: 每种状态「下一步做什么」。账号页与通知都用这一份，避免口径不一致。
HINT: dict[str, str] = {
    STATE_VERIFIED: "可以使用候补购票。",
    STATE_UNVERIFIED: (
        "候补购票需要先完成人证核验：打开铁路 12306 App →「我的」→ 名字下方的「人证核验」"
        "→ 勾选同意后按提示做人脸识别（必须本人）。完成后回到账号管理，"
        "点该账号的「重测核验」刷新状态即可。"
    ),
    STATE_UNKNOWN: (
        "12306 没有返回核验状态，无法判定。这不代表账号有问题 —— "
        "候补会照常提交，若官方拒绝会给出真实原因。"
    ),
    STATE_UNCHECKED: "账号登录成功后会自动检测核验状态。",
}

#: 官方说明（候补购票操作说明 / 身份信息核验问答）
GUIDE_URL = "https://kyfw.12306.cn/otn/gonggao/checkIdentity.html?no=3"

#: 12306 App 里的操作路径，逐步写出来 —— 「去 App 里核验」这种话等于没说
GUIDE_STEPS: tuple[str, ...] = (
    "手机上打开「铁路 12306」App（未安装请先在应用商店搜索安装，注意认准官方）",
    "登录你的账号，点右下角「我的」",
    "在名字下方找到「人证核验」，点进去",
    "勾选同意后点「开始核验」，按提示做人脸识别（必须本人，不能代做）",
    "核验通过后回到本程序，在「账号管理」里点该账号的「重测核验」刷新状态",
)

#: 已核验 / 无法判定 都**不拦**；只有明确未核验才拦。
BLOCKING_STATES = frozenset({STATE_UNVERIFIED})


def _as_mapping(info: Any) -> Mapping[str, Any]:
    """统一接受「字典」和「User 对象」两种入参。

    调用方既有 ``User.to_dict()`` 的结果，也有直接拿着 ``User`` 对象的
    （候补引擎、诊断脚本）——与其让每处都手工拼一个字典，
    不如在这里做一次适配，顺手把「字段名」这件事定死在一个地方。
    """
    if info is None:
        return {}
    if isinstance(info, Mapping):
        return info
    return {
        "is_checked": bool(getattr(info, "is_checked", False)),
        "verified_checked": bool(getattr(info, "verified_checked", False)),
        "verify_known": bool(getattr(info, "verify_known", False)),
    }


def state_of(info: Any) -> str:
    """从账号信息里读出核验状态（四态）。

    ``info`` 可以是 ``User.to_dict()`` 的输出、``User`` 对象本身，
    或任意含 ``is_checked`` / ``verified_checked`` / ``verify_known`` 三个键的映射。
    """
    data = _as_mapping(info)
    if data.get("is_checked"):
        return STATE_VERIFIED
    if not data.get("verified_checked"):
        return STATE_UNCHECKED
    return STATE_UNVERIFIED if data.get("verify_known") else STATE_UNKNOWN


def label_of(info: Any) -> str:
    """状态的中文名（「已核验」/「未核验」/「无法判定」/「未检测」）。"""
    return LABEL[state_of(info)]


def hint_of(info: Any) -> str:
    """该状态下「下一步做什么」。"""
    return HINT[state_of(info)]


def should_block(info: Any) -> bool:
    """是否应当**拦下候补**。只有「明确未核验」为真。

    这一条是整个模块的重点：把「判不出来」当成「未核验」，
    会拦掉一个本来能用的账号 —— 真机实测官方两个用户信息接口
    **都不返回核验字段**，所以那种拦法是必然会发生的误伤。
    """
    return state_of(info) in BLOCKING_STATES


def standby_block_message(username: str) -> str:
    """候补被核验拦截时的完整文案（带可照做的出路）。

    以前只有「请先在 12306 完成核验」一句 —— 使用者不知道该去哪儿点。
    """
    return (
        f"账号「{username}」未完成人证核验，12306 不允许提交候补订单。\n"
        f"怎么解决：{HINT[STATE_UNVERIFIED]}\n"
        f"官方说明：{GUIDE_URL}"
    )


def guide_lines() -> list[str]:
    """「怎么核验」的逐步说明（给界面上的说明弹窗/提示用）。"""
    steps = [f"{index}. {text}" for index, text in enumerate(GUIDE_STEPS, start=1)]
    steps.append(f"官方说明：{GUIDE_URL}")
    return steps


#: 官方在提交候补被拒时的措辞里，出现这些词就说明是核验问题 ——
#: 用来把官方那句生硬的错误翻成「去哪儿点两下」。
UNVERIFIED_KEYWORDS = (
    "人证核验",
    "人证一致性",
    "核验",
    "实名认证",
    "未通过实名",
)


def is_unverified_error(message: str) -> bool:
    """官方报错里是否指向「人证核验未完成」。"""
    text = str(message or "")
    return any(keyword in text for keyword in UNVERIFIED_KEYWORDS)
