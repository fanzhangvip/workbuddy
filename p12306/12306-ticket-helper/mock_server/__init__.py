"""本地 mock-12306 服务（离线端到端演练与自动化测试用）。"""

from mock_server.server import MOCK_PASSENGERS, SCENARIOS, MockState, create_mock_app, run_mock_server

__all__ = ["MOCK_PASSENGERS", "SCENARIOS", "MockState", "create_mock_app", "run_mock_server"]
