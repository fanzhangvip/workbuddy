"""配置文件模板（CF-01 / CF-03）。

使用方法：
    1. 复制本文件到用户目录下的 ``env.py``：
       - Windows: ``%USERPROFILE%\\.ticket-helper\\env.py``
       - macOS/Linux: ``~/.ticket-helper/env.py``
       或者直接执行 ``python scripts/init_env.py`` 自动完成复制。
    2. 按需修改下面的值。**真实密钥只写在这里或环境变量中，绝不写进代码库。**

注意：本文件里的占位值（``"********"``、空字符串）都不会被提交到版本库，
真实 ``env.py`` 已被 .gitignore 忽略。
"""

ENV = {
    # ---------------- 基础 ---------------- #
    # 接口基址。指向本地 mock（如 http://127.0.0.1:18006）即可离线端到端演练
    "api_base": "https://kyfw.12306.cn",
    "log_level": "INFO",

    # ---------------- 账号（AC-01） ---------------- #
    # 密码在写入数据库时会由密钥文件加密，不会以明文落盘
    "users": [
        # {"username": "your_account", "password": "your_password", "heartbeat": 120, "enabled": True},
    ],

    # ---------------- 抢票任务（4.3） ---------------- #
    "tasks": [
        # {
        #     "name": "示例：广州→长沙",
        #     "left_dates": ["2026-10-01", "2026-10-02"],
        #     "from_stations": ["广州南"],
        #     "to_stations": ["长沙南"],
        #     "trains_whitelist": ["G6102", "G6140"],   # 白名单即优先级顺序
        #     "trains_blacklist": [],
        #     "train_types": [],                        # 空=不限；可填 高铁/动车/直达/特快/快速/普快
        #     "seat_types": ["二等座", "一等座", "无座"], # 席别优先级顺序
        #     "depart_time_range": ["06:00", "23:00"],
        #     "passengers": ["张三"],
        #     "accounts": [],                           # 空=自动分配在线账号
        #     "strategy": "dual",                       # direct 直接抢 / standby 仅候补 / dual 双轨
        #     "query_interval": 1.0,
        #     "sale_open_time": "",                     # 例 "2026-10-01 08:00:00"，留空=不定时捡漏
        #     "sale_prepare_seconds": 30,
        #     "max_retry": 20,
        #     "enabled": True,
        # },
    ],

    # ---------------- 通知（NT-01 ~ NT-05） ---------------- #
    "notify": {
        "web": True,  # Web/桌面端弹窗 + 声音提醒
        "email": {
            "enabled": False,
            "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "use_ssl": True,
            "username": "",
            "password": "",          # 邮箱授权码
            "sender": "",
            "receivers": [],
        },
        "serverchan": {"enabled": False, "key": "", "webhook": ""},
        "dingtalk": {"enabled": False, "webhook": ""},
        "wecom": {"enabled": False, "webhook": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
        "voice": {"enabled": False, "provider": "aliyun", "appcode": "", "phone": "", "tts_code": ""},
    },

    # ---------------- 验证码（VC-01 ~ VC-04） ---------------- #
    "codec": {
        # free 外部打码服务 / local 本地模型 / manual 人工兜底
        # （云打码渠道已于 2026-09-29 移除，见 ticket_helper/verify/service.py）
        #
        # free 模式现在跑的是**上游 pjialin/py12306 的 user 模式**：
        #   1) 先起本机服务（识别内核 ddddocr）：
        #        python -m ticket_helper.verify.captcha_server
        #   2) 它会按上游协议把图 POST 出去，并把返回的**格子编号**
        #      交给上游 get_image_position_by_offset 换算成点击坐标。
        "mode": "free",
        "local_model_path": "",      # 形如 <home>/data/model/captcha.onnx
        # 打码服务地址。留空 = http://127.0.0.1:8899/check（本机服务默认地址）
        "user_endpoint": "",
        "retry_limit": 3,
        "fallback_enabled": True,    # 本地失败自动切云端（VC-04）
    },

    # ---------------- Web 后台（WB-07） ---------------- #
    "web": {
        "enable": True,
        "host": "127.0.0.1",         # 只监听本机；如需局域网访问改成 0.0.0.0 并务必设置强密码
        "port": 8008,
        "auth_user": "admin",
        "auth_pass": "********",     # 必填。若为空或仍是全星号，程序会自动生成随机密码
        "secret": "",                # 留空则自动从密钥文件派生
        "token_ttl": 43200,
        "strict_cors": True,
    },

    # ---------------- 反检测限速（RC-01 / RC-02） ---------------- #
    "ratelimit": {
        "adaptive": True,
        "min_interval": 1.0,
        "max_interval": 8.0,
        "proxy_pool": "",            # 逗号分隔代理，或返回代理列表的接口地址
        "proxy_rotate_every": 200,
        "risk_backoff_factor": 2.0,
        "recovery_step": 0.5,
        "recovery_after": 30,
    },

    # ---------------- 存储（4.9） ---------------- #
    "storage": {
        "db_path": "",               # 留空使用 <home>/data/app.db
        "keep_query_records_days": 7,
    },

    # ---------------- 分布式（DS-03 ~ DS-05，可选） ---------------- #
    "cluster": {
        "enabled": False,
        "redis_url": "redis://127.0.0.1:6379/0",
        "node_name": "",
        "role": "master",
    },
}
