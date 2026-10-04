"""配置数据结构（分节）。

对应设计文档 4.1 / 第七节。使用 dataclass 保证类型明确、可校验、可序列化，
便于 Web 端热更新（to_dict / from_dict 往返无损）。
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any

from ticket_helper.errors import ValidationError

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SEAT_TYPES: tuple[str, ...] = (
    "商务座",
    "特等座",
    "一等座",
    "二等座",
    "高级软卧",
    "软卧",
    "硬卧",
    "软座",
    "硬座",
    "无座",
)

TRAIN_TYPES: tuple[str, ...] = ("高铁", "动车", "直达", "特快", "快速", "普快", "其他")

STRATEGIES: tuple[str, ...] = ("direct", "standby", "dual")

# --------------------------------------------------------------------------- #
# 任务的持久化状态值（``tasks.status`` 列）
#
# **唯一的定义处。** 以前这套值散在 5 个地方各写一遍：
# ``runtime/context.py`` 的「引擎状态 → 任务状态」翻译表、
# ``scheduler/scheduler.py`` 的三处写入（其中一处还是 SQL 里的 ``status = 'running'``）、
# ``storage/repository.py`` 的默认值、``web/api.py`` 的默认值。
# 字符串相同不代表语义相同，也不代表改一处就都改到了 ——
# 所以这里集中定义，其余地方必须引用。``tests/test_contracts.py`` 盯着。
# --------------------------------------------------------------------------- #
TASK_STATUS_WAITING = "waiting"      # 等待开售 / 待命
TASK_STATUS_RUNNING = "running"      # 正在盯票
TASK_STATUS_STOPPED = "stopped"      # 已停止（含未启动与手动停止）
TASK_STATUS_FINISHED = "finished"    # 已抢到
TASK_STATUS_ERROR = "error"          # 异常终止

#: 全部合法取值（校验 / 契约测试用）
TASK_STATUSES: tuple[str, ...] = (
    TASK_STATUS_WAITING,
    TASK_STATUS_RUNNING,
    TASK_STATUS_STOPPED,
    TASK_STATUS_FINISHED,
    TASK_STATUS_ERROR,
)

#: 12306 官方候补硬约束
STANDBY_MAX_ORDERS = 6
STANDBY_MAX_DATES_PER_ORDER = 3
STANDBY_MAX_COMBOS_PER_ORDER = 60

DATE_FORMAT = "%Y-%m-%d"

#: 对外输出时对敏感字段的脱敏占位符。
#: 只用「全星号」这一种形态：配置校验会把全星号的值视为「未设置」，
#: 从而避免「占位符被当成真实密码」这类隐蔽的弱口令问题。
SecretMasked = "********"


# --------------------------------------------------------------------------- #
# 子配置
# --------------------------------------------------------------------------- #
@dataclass
class RateLimitConfig:
    """反检测限速（RC-01/RC-02）。"""

    adaptive: bool = True
    min_interval: float = 1.0
    max_interval: float = 8.0
    proxy_pool: str = ""
    proxy_rotate_every: int = 200
    risk_backoff_factor: float = 2.0
    recovery_step: float = 0.5
    recovery_after: int = 30

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.min_interval <= 0:
            errors.append("ratelimit.min_interval 必须大于 0")
        if self.max_interval < self.min_interval:
            errors.append("ratelimit.max_interval 不能小于 min_interval")
        return errors


@dataclass
class CodecConfig:
    """验证码识别（VC-01~VC-04）。"""

    #: free / local / manual
    #: （``cloud`` 云打码渠道已于 2026-09-29 移除，理由见 ``verify/service.py`` 的模块注释）
    mode: str = "free"
    local_model_path: str = ""
    retry_limit: int = 3
    fallback_enabled: bool = True
    request_timeout: float = 15.0

    #: 打码服务地址（上游 `user` 模式）。
    #:
    #: **留空 = 用内核默认**（本机服务 ``http://127.0.0.1:8899/check``，
    #: 由 ``python -m ticket_helper.verify.captcha_server`` 启动）。
    #:
    #: 为什么要可配：上游客户端的地址协议是「POST 图 → 拿回格子编号」，
    #: 换服务就是要换地址。写死地址会让「换一个打码服务」变成改源码。
    user_endpoint: str = ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.mode == "cloud":
            # 云打码渠道已删除（原实现任何配置下都不可能成功：账号空、且从不真正上传图片，
            # 见 ``verify/service.py`` 的模块注释）。
            # 这里必须**明确报错**，不能静默降级成 manual —— 使用者会以为还在用云打码，
            # 实际什么都不会发生。那正是这次要根除的那类「静默失效」。
            errors.append(
                "codec.mode=cloud 已不再支持：云打码渠道已移除（原实现从不真正上传验证码图片）。"
                "请改为 manual（人工兜底；登录请用扫码）或 local（自备 onnx 模型）"
            )
        elif self.mode not in ("free", "local", "manual"):
            errors.append(f"codec.mode 取值非法: {self.mode}")
        if self.retry_limit < 1:
            errors.append("codec.retry_limit 至少为 1")
        if self.mode == "local" and not self.local_model_path:
            errors.append("codec.mode=local 时必须配置 local_model_path")
        return errors


@dataclass
class StorageConfig:
    db_path: str = ""
    keep_query_records_days: int = 7
    vacuum_on_start: bool = False

    def validate(self) -> list[str]:
        if self.keep_query_records_days < 1:
            return ["storage.keep_query_records_days 至少为 1"]
        return []


@dataclass
class WebConfig:
    enable: bool = True
    host: str = "127.0.0.1"
    port: int = 8008
    auth_user: str = "admin"
    auth_pass: str = ""
    secret: str = ""
    token_ttl: int = 12 * 3600
    #: 生产环境务必保持 True；显式开启才允许通配来源
    strict_cors: bool = True

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not (1 <= int(self.port) <= 65535):
            errors.append("web.port 超出合法范围")
        return errors

    #: 脱敏占位符：所有字段在对外输出时都会被替换成它。
    #: 关键设计：**全星号的值一律视为「未设置」**，这样即使模板占位符被误当成真实密码，
    #: 启动时也会被识别并重新生成，而不会留下一个可预测的弱口令。
    MASK = "********"

    def is_password_missing(self) -> bool:
        value = (self.auth_pass or "").strip()
        return (not value) or set(value) == {"*"}

    def to_dict_masked(self) -> dict[str, Any]:
        """脱敏序列化（Web 端与桌面端读配置时使用）。"""
        data = asdict(self)
        data["auth_pass"] = SecretMasked if data.get("auth_pass") else ""
        data["secret"] = SecretMasked if data.get("secret") else ""
        return data

    def security_problems(self) -> list[str]:
        """安全性检查（不阻塞启动，但必须显著告警）。

        单独抽出来而不是塞进 ``validate()``：校验失败会直接阻断配置加载，
        而「后台密码为空」是可以自动补救的（由 AppContext 生成随机密码），
        把它变成硬崩溃并不划算。
        """
        problems: list[str] = []
        if self.enable and self.is_password_missing():
            problems.append("web.auth_pass 为空或仍是占位符，系统将自动生成随机密码")
        if self.enable and self.host not in ("127.0.0.1", "localhost", "::1"):
            problems.append(
                f"web.host={self.host} 监听非本机地址，后台可能暴露到公网，请务必设置强密码并启用防火墙"
            )
        if not self.strict_cors:
            problems.append("web.strict_cors=False 允许任意来源跨域访问，生产环境应保持 True")
        return problems

    @property
    def allowed_origins(self) -> list[str]:
        """CORS 显式来源列表（生产禁用通配符）。"""
        origins = [
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
            "http://127.0.0.1:5173",
            "null",
        ]
        if not self.strict_cors:
            origins.append("*")
        return origins


@dataclass
class ClusterConfig:
    """分布式（DS-03~DS-05）。单机默认关闭。"""

    enabled: bool = False
    redis_url: str = "redis://127.0.0.1:6379/0"
    node_name: str = ""
    role: str = "master"
    lock_ttl: int = 120

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.enabled and not self.redis_url:
            errors.append("cluster.enabled=True 时必须配置 redis_url")
        if self.role not in ("master", "slave"):
            errors.append("cluster.role 只能是 master / slave")
        return errors


@dataclass
class EmailNotifyConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    sender: str = ""
    receivers: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        if not self.enabled:
            return []
        errors: list[str] = []
        if not self.smtp_host:
            errors.append("notify.email.smtp_host 不能为空")
        if not self.username:
            errors.append("notify.email.username 不能为空")
        if not self.receivers:
            errors.append("notify.email.receivers 至少配置一个收件人")
        return errors


@dataclass
class WebhookNotifyConfig:
    """Server酱 / 钉钉 / Telegram / 企业微信 通用 webhook 型渠道。"""

    enabled: bool = False
    key: str = ""
    webhook: str = ""

    def validate(self) -> list[str]:
        if self.enabled and not (self.key or self.webhook):
            return ["启用的 webhook 渠道需要配置 key 或 webhook 地址"]
        return []


@dataclass
class TelegramNotifyConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    #: 接口地址覆盖。**留空 = 用官方地址**。
    #: 存在的理由：不可覆盖的硬编码 URL **无法被 F4「能力可用性证明」见证**，
    #: 而本项目已经吃过一次「看起来配好了、请求其实没发出去」的亏。
    endpoint: str = ""

    def validate(self) -> list[str]:
        if self.enabled and not (self.bot_token and self.chat_id):
            return ["notify.telegram 启用时需要 bot_token 与 chat_id"]
        return []


@dataclass
class VoiceNotifyConfig:
    """电话语音通知（NT-03），仅在重大事件触发。"""

    enabled: bool = False
    provider: str = "aliyun"
    appcode: str = ""
    phone: str = ""
    tts_code: str = ""
    #: 接口地址覆盖（与 telegram.endpoint 同理由：让「请求真的发出去了」可被见证）
    endpoint: str = ""

    def validate(self) -> list[str]:
        if self.enabled and not (self.appcode and self.phone):
            return ["notify.voice 启用时需要 appcode 与 phone"]
        return []


@dataclass
class NotifyConfig:
    email: EmailNotifyConfig = field(default_factory=EmailNotifyConfig)
    serverchan: WebhookNotifyConfig = field(default_factory=WebhookNotifyConfig)
    dingtalk: WebhookNotifyConfig = field(default_factory=WebhookNotifyConfig)
    wecom: WebhookNotifyConfig = field(default_factory=WebhookNotifyConfig)
    telegram: TelegramNotifyConfig = field(default_factory=TelegramNotifyConfig)
    voice: VoiceNotifyConfig = field(default_factory=VoiceNotifyConfig)
    #: Web 端弹窗 / 声音提醒开关（NT-04）
    web: bool = True

    def validate(self) -> list[str]:
        errors: list[str] = []
        for name in ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice"):
            errors.extend(getattr(self, name).validate())
        return errors


@dataclass
class UserConfig:
    """单个 12306 账号。password 为明文的内存态，落库时由 crypto 加密。"""

    username: str = ""
    password: str = ""
    heartbeat: int = 120
    enabled: bool = True

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.username:
            errors.append("账号用户名不能为空")
        if self.heartbeat < 30:
            errors.append(f"账号 {self.username} 的心跳间隔不得小于 30s")
        return errors


@dataclass
class TaskConfig:
    """一个抢票任务。"""

    id: int | None = None
    name: str = ""
    left_dates: list[str] = field(default_factory=list)
    from_stations: list[str] = field(default_factory=list)
    to_stations: list[str] = field(default_factory=list)
    trains_whitelist: list[str] = field(default_factory=list)
    trains_blacklist: list[str] = field(default_factory=list)
    train_types: list[str] = field(default_factory=list)
    seat_types: list[str] = field(default_factory=list)
    depart_time_range: list[str] = field(default_factory=list)  # ["06:00", "23:00"]
    passengers: list[str] = field(default_factory=list)
    #: direct 直接抢 | standby 仅候补 | dual 候补+捡漏双轨
    strategy: str = "direct"
    query_interval: float = 1.0
    #: 准点秒杀的预售开售时刻 "2026-10-01 08:00:00"，为空表示不定时
    sale_open_time: str = ""
    #: 提前多少秒进入待命状态
    sale_prepare_seconds: int = 30
    max_retry: int = 20
    #: 指定使用的账号用户名；为空表示由调度器自动分配
    accounts: list[str] = field(default_factory=list)
    enabled: bool = True
    #: 持久化的任务状态；取值必须来自 ``TASK_STATUSES``（别再写字面量）
    status: str = TASK_STATUS_STOPPED
    priority: int = 0
    remark: str = ""

    # -------------------------------------------------------------- #
    # 校验
    # -------------------------------------------------------------- #
    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.name.strip():
            errors.append("任务名称不能为空")
        if not self.left_dates:
            errors.append(f"任务[{self.name}]必须至少配置一个乘车日期")
        from ticket_helper import train_date as train_date_util

        for date in self.left_dates:
            try:
                from datetime import datetime

                datetime.strptime(date, DATE_FORMAT)
            except (ValueError, TypeError):
                errors.append(f"任务[{self.name}]日期格式非法: {date}（应为 YYYY-MM-DD）")
                continue
            # ★ 「已经过去」是**确定查不到**的（等多久都不会变），所以在这里直接拦住
            # **保存** —— 而不是等到任务跑起来才由引擎拒绝启动。
            #
            # 为什么放在 `validate()`：`AppContext.save_task` 会因 validate 报错而
            # 拒绝写入，所以它是**唯一闸门**；两端各写一遍校验一定会漏一边
            # （实测：任务 2 的乘车日期是去年的 2025-10-03，就是这样存进去的。
            #  界面上的 `date_hints` 只是**事后**提示，拦不住写入）。
            #
            # ⚠️ **只拦「已过去」，绝不拦「未开售」**：未开售会随时间变成可查
            # （判据同 `train_date.classify`，措辞用它自己的 `explain`，不另写一套）。
            if train_date_util.classify(date) == train_date_util.DATE_PAST:
                errors.append(
                    f"任务[{self.name}]的乘车日期已经过去: {date}"
                    f"（{train_date_util.explain(date)}）"
                )
        if not self.from_stations:
            errors.append(f"任务[{self.name}]必须配置出发站")
        if not self.to_stations:
            errors.append(f"任务[{self.name}]必须配置到达站")
        if not self.seat_types:
            errors.append(f"任务[{self.name}]必须配置席别优先级")
        for seat in self.seat_types:
            if seat not in SEAT_TYPES:
                errors.append(f"任务[{self.name}]不支持的席别: {seat}")
        if self.strategy not in STRATEGIES:
            errors.append(f"任务[{self.name}]策略非法: {self.strategy}")
        if self.query_interval < 0.3:
            errors.append(f"任务[{self.name}]查询间隔不得小于 0.3s（避免触发风控）")
        if self.depart_time_range:
            if len(self.depart_time_range) != 2:
                errors.append(f"任务[{self.name}]出发时间区间需要两个值，如 [\"06:00\",\"23:00\"]")
        for forbid in self.trains_blacklist:
            if forbid and forbid in self.trains_whitelist:
                errors.append(f"任务[{self.name}]车次 {forbid} 同时存在于白名单与黑名单")
        if self.max_retry < 1:
            errors.append(f"任务[{self.name}]max_retry 至少为 1")
        return errors

    def combos(self) -> list[tuple[str, str, str]]:
        """展开 (出发站, 到达站, 日期) 组合。"""
        return [
            (f, t, d)
            for d in self.left_dates
            for f in self.from_stations
            for t in self.to_stations
        ]

    def standby_combo_count(self, train_count: int) -> int:
        """估算候补「日期+车次」组合数，用于官方上限校验。"""
        station_pairs = max(1, len(self.from_stations) * len(self.to_stations))
        return len(self.left_dates) * max(1, train_count) * station_pairs


# --------------------------------------------------------------------------- #
# 根配置
# --------------------------------------------------------------------------- #
@dataclass
class AppConfig:
    """应用总配置。"""

    users: list[UserConfig] = field(default_factory=list)
    tasks: list[TaskConfig] = field(default_factory=list)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    cluster: ClusterConfig = field(default_factory=ClusterConfig)
    web: WebConfig = field(default_factory=WebConfig)
    ratelimit: RateLimitConfig = field(default_factory=RateLimitConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    codec: CodecConfig = field(default_factory=CodecConfig)
    #: 接口层基址，指向 mock-12306 时即可离线端到端跑通（可注入覆盖）
    api_base: str = "https://kyfw.12306.cn"
    #: 全局日志级别
    log_level: str = "INFO"

    # -------------------------------------------------------------- #
    def validate(self) -> list[str]:
        """校验必填项，返回错误列表（CF-03）。"""
        errors: list[str] = []
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append(f"log_level 非法: {self.log_level}")
        errors.extend(self.web.validate())
        errors.extend(self.ratelimit.validate())
        errors.extend(self.storage.validate())
        errors.extend(self.codec.validate())
        errors.extend(self.notify.validate())
        errors.extend(self.cluster.validate() if self.cluster.enabled else [])

        usernames = [u.username for u in self.users if u.username]
        dup = {name for name in usernames if usernames.count(name) > 1}
        for name in dup:
            errors.append(f"账号 {name} 重复配置")

        for user in self.users:
            errors.extend(user.validate())
        for task in self.tasks:
            errors.extend(task.validate())
            for account in task.accounts:
                if usernames and account not in usernames:
                    errors.append(f"任务[{task.name}]引用了不存在的账号: {account}")
        return errors

    def raise_if_invalid(self) -> None:
        """启动时快速失败。"""
        errors = self.validate()
        if errors:
            raise ValidationError("配置校验未通过: " + "; ".join(errors))

    # -------------------------------------------------------------- #
    def to_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """序列化。默认掩码敏感字段，避免泄漏到日志 / 前端。"""
        data = asdict(self)
        if not mask_secrets:
            return data

        for user in data.get("users", []):
            if user.get("password"):
                user["password"] = SecretMasked
        notify = data.get("notify", {})
        for section in ("email", "serverchan", "dingtalk", "wecom", "telegram", "voice"):
            block = notify.get(section) or {}
            for key in ("password", "key", "webhook", "appcode", "bot_token", "chat_id", "phone"):
                if block.get(key):
                    block[key] = SecretMasked
        data["web"] = self.web.to_dict_masked()
        if data.get("cluster", {}).get("redis_url"):
            data["cluster"]["redis_url"] = SecretMasked
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        """从 dict 还原（供 Web 热更新 / env.py 加载）。未知字段忽略，缺失字段取默认值。"""
        payload = copy.deepcopy(data or {})
        config = cls()
        for name, value in payload.items():
            if not hasattr(config, name) or value is None:
                continue
            # 列表型字段需要按元素类型还原成 dataclass，否则下游会拿到裸 dict
            if name == "users":
                config.users = [
                    UserConfig(**_filter_kwargs(UserConfig, item)) for item in value if isinstance(item, dict)
                ]
                continue
            if name == "tasks":
                config.tasks = [
                    TaskConfig(**_filter_kwargs(TaskConfig, item)) for item in value if isinstance(item, dict)
                ]
                continue
            setattr(config, name, _coerce(getattr(config, name), value))
        return config


def _coerce(current: Any, value: Any) -> Any:
    """按当前字段类型做一次浅转换，避免前端传来字符串导致类型错乱。"""
    if isinstance(current, UserConfig):
        return UserConfig(**_filter_kwargs(UserConfig, value))
    if isinstance(current, TaskConfig):
        return TaskConfig(**_filter_kwargs(TaskConfig, value))
    if isinstance(current, NotifyConfig):
        cfg = NotifyConfig()
        for key, sub in (value or {}).items():
            if key == "web":
                cfg.web = bool(sub)
            elif hasattr(cfg, key):
                setattr(cfg, key, _coerce(getattr(cfg, key), sub))
        return cfg
    if isinstance(current, (EmailNotifyConfig, WebhookNotifyConfig, TelegramNotifyConfig, VoiceNotifyConfig)):
        return type(current)(**_filter_kwargs(type(current), value))
    if isinstance(current, (WebConfig, ClusterConfig, RateLimitConfig, StorageConfig, CodecConfig)):
        return type(current)(**_filter_kwargs(type(current), value))
    if isinstance(current, list):
        return list(value)
    if isinstance(current, float) and isinstance(value, (int, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return current
    if isinstance(current, int) and not isinstance(current, bool) and isinstance(value, str):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return current
    if isinstance(current, bool) and isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value


def _filter_kwargs(dc: type, value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = set(dc.__dataclass_fields__)  # type: ignore[attr-defined]
    return {k: v for k, v in value.items() if k in allowed and v is not None}
