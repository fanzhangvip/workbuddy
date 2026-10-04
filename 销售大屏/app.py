"""
销售数据大屏 —— Flask 应用入口。

本项目采用增量开发：每一步只做当前功能，不提前开发后续内容。
当前阶段：大屏数据接口 + 数据管理后台。
"""

from flask import Flask, render_template, jsonify, request

import config
from core import analytics, analyst, datastore


def create_app():
    """应用工厂：方便后续注册蓝图 / 扩展模块，无需改动已有代码。"""
    app = Flask(__name__)

    # 让 jsonify 直接输出中文，而不是 \uXXXX 转义
    app.json.ensure_ascii = False

    register_routes(app)
    return app


def register_routes(app):
    """注册基础路由。后续新增接口时在此追加，不改动已有路由。"""

    @app.route("/")
    def index():
        """大屏首页。"""
        return render_template("dashboard.html")

    @app.route("/health")
    def health():
        """健康检查：用于确认服务正常启动。"""
        return jsonify({"status": "ok"})

    @app.route("/api/dashboard")
    def api_dashboard():
        """
        大屏数据总接口：一次性返回全部图表所需数据。

        返回格式统一为 {"code": 0, "data": {...}, "msg": ""}
        无数据时 data.empty 为 True，前端显示「暂无数据」。
        """
        try:
            return jsonify({"code": 0, "data": analytics.dashboard(), "msg": ""})
        except Exception as exc:  # noqa: BLE001  数据异常时给前端明确提示，而不是 500 白屏
            return jsonify({"code": 1, "data": None, "msg": str(exc)}), 500

    # ---------------- 数据管理后台 ----------------

    @app.route("/admin")
    def admin():
        """数据管理后台页面。"""
        return render_template("admin.html")

    @app.route("/api/data/info")
    def api_data_info():
        """数据文件信息：文件名、大小、行列数、字段完整性。"""
        return jsonify({"code": 0, "data": datastore.info(), "msg": ""})

    @app.route("/api/data/preview")
    def api_data_preview():
        """预览数据前若干行，行数由 limit 参数控制。"""
        limit = request.args.get("limit", default=15, type=int)
        limit = max(1, min(limit, 100))   # 限制范围，避免一次拉太多
        return jsonify({"code": 0, "data": datastore.preview(limit), "msg": ""})

    @app.route("/api/data/upload", methods=["POST"])
    def api_data_upload():
        """上传数据文件：校验必需字段后覆盖当前数据。"""
        file = request.files.get("file")
        if file is None:
            return jsonify({"code": 1, "data": None, "msg": "未收到文件，请重新选择"}), 400

        result = datastore.save_upload(file)
        return jsonify({
            "code": 0 if result["ok"] else 1,
            "data": result,
            "msg": result["msg"],
        })

    @app.route("/api/data", methods=["DELETE"])
    def api_data_delete():
        """删除当前数据文件，大屏将显示暂无数据。"""
        result = datastore.delete()
        return jsonify({
            "code": 0 if result["ok"] else 1,
            "data": result,
            "msg": result["msg"],
        })

    # ---------------- AI 数据分析师 ----------------

    @app.route("/analyst")
    def analyst_page():
        """AI 数据分析师问答页面。"""
        return render_template(
            "analyst.html",
            configured=analyst.is_configured(),
            model=config.DEEPSEEK_MODEL,
        )

    @app.route("/api/ai/ask", methods=["POST"])
    def api_ai_ask():
        """向 AI 数据分析师提问。请求体：{"question": str, "history": [...]}"""
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        history = payload.get("history") or []

        if not question:
            return jsonify({"code": 1, "data": None, "msg": "请输入问题"}), 400

        try:
            result = analyst.ask(question, history)
            return jsonify({"code": 0, "data": result, "msg": ""})
        except Exception as exc:  # noqa: BLE001  模型/网络异常时给前端明确提示
            return jsonify({"code": 1, "data": None, "msg": f"AI 调用失败：{exc}"}), 500

    @app.route("/api/ai/brief", methods=["POST"])
    def api_ai_brief():
        """
        大屏右下角悬浮机器人专用：简报模式。

        返回简短结论 + 关键数据依据 + 本次实际执行的 Pandas 表达式。
        """
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        history = payload.get("history") or []

        if not question:
            return jsonify({"code": 1, "data": None, "msg": "请输入问题"}), 400

        try:
            return jsonify({"code": 0, "data": analyst.ask_brief(question, history), "msg": ""})
        except Exception as exc:  # noqa: BLE001
            return jsonify({"code": 1, "data": None, "msg": f"AI 调用失败：{exc}"}), 500


# 模块级 app 实例，便于 `flask run` 或 WSGI 部署
app = create_app()


if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
