"""验证码识别模块。"""

from ticket_helper.verify.service import (
    CallbackSolver,
    CaptchaSolver,
    CaptchaTask,
    FreeSolver,
    LocalModelSolver,
    ManualSolver,
    VerifyCodeService,
)

__all__ = [
    "CallbackSolver",
    "CaptchaSolver",
    "CaptchaTask",
    "FreeSolver",
    "LocalModelSolver",
    "ManualSolver",
    "VerifyCodeService",
]
