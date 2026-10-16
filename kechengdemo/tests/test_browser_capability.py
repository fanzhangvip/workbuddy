# -*- coding: utf-8 -*-
"""浏览器能力判定（容器部署的边界）—— 修「扫码登录按钮抛裸 Errno」。

    .venv\\Scripts\\python.exe tests\\test_browser_capability.py

## 这个文件在守什么

后端既可能跑在宿主机（有 Chrome、有桌面），也可能跑在容器里（都没有）。改造前
三个入口各自直接 `Popen(chrome.exe)`，容器里的表现是::

    [Errno 2] No such file or directory: 'C:\\Program Files\\...\\chrome.exe'

用户既看不懂、也不知道下一步该做什么。现在判定收敛到
`wb_bootstrap.browser_env()`，本文件锁住三件事：

  1. **判定口径**：显式关闭 / 容器 / Chrome 不存在 → `available=False`，且给出宿主机指引；
  2. **不再抛裸 Errno**：`launch_chrome` 抛 `BrowserUnavailable`（带 reason+hint）；
  3. **接口层不再"假启动"**：`auth_service.start_login` 在不可用时**不启动线程**、
     **不把状态打成 failed**；`wb_keepalive.refresh` 落 `code=4` 且消息里没有 Errno。

需要 PostgreSQL（`credential_status()` / `status()` 会查 `credential` 表）。
"""
#: 测试分层（`tests/run_all.py` 解析本行；忘了声明会被拒绝执行）
#: t0 = 纯逻辑：不碰 PG / 网络 ｜ t1 = 需要本地 PostgreSQL(5433) ｜ t2 = 真实外部依赖（真模型 / 真微伴企微 / Chrome）—— 默认不跑
TIER = "t1"

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from _harness import ROOT, check, section, summary          # noqa: E402

sys.path.insert(0, str(ROOT))

import wb_bootstrap                                          # noqa: E402

_ENV_KEYS = ("CHROME_PATH", "BROWSER_ENABLED", "BROWSER_IN_CONTAINER")


@contextmanager
def env(**kv):
    """临时改环境变量，退出时**逐键还原**（含"原本不存在"的情形）。"""
    old = {k: os.environ.get(k) for k in _ENV_KEYS}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_chrome_path_resolution():
    section("Chrome 路径解析（CHROME_PATH > 注册表 > 常见位置）")
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "chrome.exe"
        fake.write_text("stub", encoding="utf-8")

        with env(CHROME_PATH=str(fake)):
            path, source = wb_bootstrap.chrome_path()
            check("CHROME_PATH 指向真实文件 → 采用它",
                  path == str(fake) and source == "env:CHROME_PATH", (path, source))

        # 指向不存在的文件 → **不得**把它当结果（否则等于把错配置静默吃下去）
        with env(CHROME_PATH=str(Path(tmp) / "nope.exe")):
            path, source = wb_bootstrap.chrome_path()
            check("CHROME_PATH 指向不存在的文件 → 不采用它",
                  path != str(Path(tmp) / "nope.exe"), (path, source))
            benv = wb_bootstrap.browser_env()
            if benv["available"] is False and "Chrome" in benv["reason"]:
                check("找不到 Chrome 时的原因里带出候选路径",
                      "nope.exe" in benv["reason"], benv["reason"])


def test_browser_env_decisions():
    section("browser_env：三种「不可用」的判定与指引")
    # 宿主机上显式关闭：指引应该是"把开关去掉"，**不能**是去宿主机执行（我们就在宿主机）
    with env(BROWSER_ENABLED="0", BROWSER_IN_CONTAINER="0"):
        benv = wb_bootstrap.browser_env()
        check("BROWSER_ENABLED=0 → 不可用", benv["available"] is False, benv)
        check("理由点名 BROWSER_ENABLED", "BROWSER_ENABLED" in benv["reason"], benv["reason"])
        check("指引是「去掉开关」（而不是让用户换机器）",
              "BROWSER_ENABLED" in benv["hint"], benv["hint"])

    with env(BROWSER_ENABLED=None, BROWSER_IN_CONTAINER="1"):
        benv = wb_bootstrap.browser_env()
        check("容器内 → 不可用", benv["available"] is False, benv)
        check("理由说明容器缺 Chrome/显示器",
              "容器" in benv["reason"] and "Chrome" in benv["reason"], benv["reason"])
        check("指引里给出宿主机要执行的命令",
              wb_bootstrap.HOST_LOGIN_CMD in benv["hint"], benv["hint"])
        check("指引里说明凭据落 .cache/",
              ".cache" in benv["hint"], benv["hint"])
        # 容器里 compose 同时会设 BROWSER_ENABLED=0 —— 先判容器，指引必须仍是"去宿主机"
        with env(BROWSER_ENABLED="0"):
            benv2 = wb_bootstrap.browser_env()
            check("容器内即使 BROWSER_ENABLED=0，指引仍是「去宿主机」",
                  wb_bootstrap.HOST_LOGIN_CMD in benv2["hint"], benv2["hint"])

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "chrome.exe"
        fake.write_text("stub", encoding="utf-8")
        # 显式声明"不在容器里"，否则本用例在容器内跑会得出相反结论
        with env(CHROME_PATH=str(fake), BROWSER_ENABLED=None, BROWSER_IN_CONTAINER="0"):
            benv = wb_bootstrap.browser_env()
            check("宿主机 + Chrome 存在 → 可用", benv["available"] is True, benv)
            check("可用时 chrome 字段就是解析出的路径",
                  benv["chrome"] == str(fake), benv["chrome"])


def test_launch_raises_clear_error():
    section("launch_chrome：不留裸 Errno")
    with env(BROWSER_ENABLED=None, BROWSER_IN_CONTAINER="1"):
        try:
            wb_bootstrap.launch_chrome("about:blank")
            check("不可用时 launch_chrome 抛异常", False, "居然没抛")
        except wb_bootstrap.BrowserUnavailable as e:
            check("抛的是 BrowserUnavailable（不是 FileNotFoundError）", True)
            check("异常带 reason", bool(e.reason), e.reason)
            check("异常带 hint（含宿主机命令）",
                  wb_bootstrap.HOST_LOGIN_CMD in e.hint, e.hint)
            check("异常信息里没有裸 Errno", "Errno" not in str(e), str(e))
        except FileNotFoundError as e:                       # 正是要消灭的那种报错
            check("抛的是 BrowserUnavailable（不是 FileNotFoundError）", False, str(e))


def test_auth_service_does_not_fake_start():
    section("auth_service：不可用时不启动流程、不打 failed")
    import auth_service

    with env(BROWSER_ENABLED=None, BROWSER_IN_CONTAINER="1"):
        auth_service._state.update(running=False, stage="idle", message="尚未登录", error=None)
        res = auth_service.start_login(wait_sec=1)
        check("start_login 返回 ok=False", res.get("ok") is False, res)
        check("返回里带 reason", bool(res.get("reason")), res)
        check("返回里带 hint（该去宿主机跑什么）",
              wb_bootstrap.HOST_LOGIN_CMD in (res.get("hint") or ""), res.get("hint"))
        st = auth_service.get_state()
        check("状态**没有**被打成 failed（不制造假故障）",
              st.get("stage") != "failed", st)
        check("也没有真的开始跑（running=False）", st.get("running") is False, st)

        info = auth_service.credential_status()
        check("credential_status 下发浏览器能力给前端",
              info.get("browser", {}).get("available") is False, info.get("browser"))


def test_keepalive_reports_environment():
    section("wb_keepalive：落 code=4，而不是抓取失败")
    import wb_keepalive

    with env(BROWSER_ENABLED=None, BROWSER_IN_CONTAINER="1"):
        res = wb_keepalive.refresh(wait_sec=1, source="test")
        check("refresh 返回 ok=False", res.get("ok") is False, res)
        check("失败码是 4（环境无浏览器，处置=换地方执行）",
              res.get("code") == wb_keepalive.CODE_NO_BROWSER, res.get("code"))
        check("消息说明「需要浏览器」", "浏览器" in (res.get("message") or ""), res.get("message"))
        check("消息里没有裸 Errno", "Errno" not in (res.get("message") or ""), res.get("message"))
        check("消息里给出宿主机指引",
              wb_bootstrap.HOST_LOGIN_CMD in (res.get("hint") or ""), res.get("hint"))

        st = wb_keepalive.status()
        check("status 下发 available=False（供前端/运维判断）",
              st["browser"]["available"] is False, st["browser"])
        check("status 的 last_run 记到这次环境原因",
              st["last_run"].get("code") == wb_keepalive.CODE_NO_BROWSER, st["last_run"])


def test_single_source_of_truth():
    section("单一来源：不允许多处各判一遍")
    import wb_chat_export
    check("导出脚本复用同一个 browser_env（没抄一份）",
          wb_chat_export.browser_env is wb_bootstrap.browser_env)
    check("导出脚本复用同一个 launch_chrome",
          wb_chat_export.launch_chrome is wb_bootstrap.launch_chrome)


def main():
    test_chrome_path_resolution()
    test_browser_env_decisions()
    test_launch_raises_clear_error()
    test_auth_service_does_not_fake_start()
    test_keepalive_reports_environment()
    test_single_source_of_truth()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
