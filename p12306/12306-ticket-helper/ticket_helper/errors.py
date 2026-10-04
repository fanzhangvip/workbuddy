"""统一类型化错误体系。

设计要点：
- 所有业务错误继承 :class:`TicketHelperError`，便于全局错误处理中间件统一捕获；
- ``code`` 为机器可读的稳定标识，``http_status`` 用于 Web 层映射；
- 错误信息只暴露可安全展示的文案，内部细节走日志，绝不返回堆栈给客户端。
"""

from __future__ import annotations

from typing import Any


class TicketHelperError(Exception):
    """项目根异常。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str = "", *, detail: str | None = None, **extra: Any) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.detail = detail
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        """转为可安全返回给客户端的结构（不含堆栈、不含内部细节）。"""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.extra:
            payload["extra"] = self.extra
        return payload

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<{self.__class__.__name__} code={self.code} message={self.message!r}>"


# --------------------------------------------------------------------------- #
# 配置 / 输入
# --------------------------------------------------------------------------- #
class ConfigError(TicketHelperError):
    code = "config_error"
    http_status = 500


class ValidationError(TicketHelperError):
    """入参校验失败（边界处校验不通过）。"""

    code = "validation_error"
    http_status = 400


class NotFoundError(TicketHelperError):
    code = "not_found"
    http_status = 404


class ConflictError(TicketHelperError):
    code = "conflict"
    http_status = 409


# --------------------------------------------------------------------------- #
# 认证 / 授权
# --------------------------------------------------------------------------- #
class AuthError(TicketHelperError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(TicketHelperError):
    code = "forbidden"
    http_status = 403


# --------------------------------------------------------------------------- #
# 12306 交互
# --------------------------------------------------------------------------- #
class InterError(TicketHelperError):
    """12306 接口层错误基类。"""

    code = "inter_error"
    http_status = 502


class NetworkError(InterError):
    code = "network_error"


class ResponseParseError(InterError):
    code = "response_parse_error"


class RiskControlError(InterError):
    """被风控拦截（频次限制 / 封禁 / 强制验证）。"""

    code = "risk_control"
    http_status = 429


class LoginError(InterError):
    code = "login_failed"


class VerifyCodeError(InterError):
    code = "verify_code_failed"


class CaptchaUnavailableError(VerifyCodeError):
    """验证码在**接口侧**就拿不到图 —— 与「识别失败」是两回事。

    为什么要分开：识别失败可以换识别源、可以重试；而接口侧拿不到图时，
    重试只是把同一个被拦的接口再打一遍，**不会变好**，还会加重风控。
    所以调用方应当据此**放弃重试**，并把结论（改用哪种登录方式）直接告诉使用者。

    真机实例：登录验证码的旧接口被 WAF 挡掉、新接口回
    ``{"status":false,"messages":["系统忙，请稍后重试"]}``。
    """

    code = "captcha_unavailable"


class SoldOutError(InterError):
    """无票 / 余票不足。"""

    code = "sold_out"
    http_status = 409


class QueueError(InterError):
    """排队中或排队异常。"""

    code = "queue_error"


class VerifyRequiredError(InterError):
    """官方要求先完成**人证核验**才能办这件事（目前只有候补）。

    为什么值得一个专门的异常类型：这是**唯一**能证明「确实需要核验」的信号，
    来自候补链路第一步 ``afterNate/chechFace`` 的 ``face_flag=false``
    （用户信息接口根本不返回核验字段，在那儿猜是猜不出来的）。

    普通 ``InterError`` 会被上层当成「提交失败，重试看看」，
    而这一类**重试一万次也没用**，必须把「去 12306 App 做核验」的出路给到使用者。
    """

    code = "verify_required"
    http_status = 400


class TrainDateUnavailableError(InterError):
    """乘车日期**不在 12306 可售范围**（已经过去 / 还没开售）。

    为什么必须与 :class:`RiskControlError` 分开：两者在真机上返回的是
    **同一张 HTML 错误页**（见 ``ticket_helper.train_date`` 的实测表），
    但处理方式完全相反：

    ==================  =========================  ==========================
    成因                 正确动作                    搞错之后的症状
    ==================  =========================  ==========================
    真风控               降频、等待、必要时换代理     —
    日期不可查           换日期（等待毫无用处）       一直报「🚨 风控」，
    ==================  =========================  ==========================

    「等一等就会好」与「等一万年也不会好」是两种错误。
    把后者报成前者，使用者会去降频、换账号、干等 —— 而真正该做的是改一个日期。
    """

    code = "train_date_unavailable"
    http_status = 400


class StandbyLimitError(TicketHelperError):
    """候补超出官方限制（6 单 / 3 日期 / 60 组合）。"""

    code = "standby_limit_exceeded"
    http_status = 400


# --------------------------------------------------------------------------- #
# 通知
# --------------------------------------------------------------------------- #
class NotifyError(TicketHelperError):
    code = "notify_error"
