"""健康检查、请求关联与受保护端点的 API 契约测试。"""

from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.infrastructure.config import Settings


class Probe:
    def __init__(self, status="healthy"):
        self.status = status

    def check(self):
        return {"mysql": self.status, "milvus": self.status}


class FakeContainer:
    """最小化的容器替身，供不需要数据库的 API 契约测试使用。"""

    def close(self) -> None:
        return None


def test_health_and_correlation():
    # 健康检查回显 X-Request-ID；未认证请求返回 401，未知端点返回 404 结构化错误体。
    with TestClient(
        create_app(Settings(_env_file=None), Probe(), container=FakeContainer())
    ) as client:
        response = client.get("/api/v1/health", headers={"X-Request-ID": "test-request"})
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.headers["X-Request-ID"] == "test-request"
        missing = client.get("/api/v1/runs/unknown")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "authentication_error"
        unknown = client.get("/api/v1/unknown-endpoint")
        assert unknown.status_code == 404
        assert set(unknown.json()["error"]) == {"code", "message", "request_id", "details"}


def test_protected_endpoints_require_authentication():
    # Conversation/Document/Data 等资源端点都必须先通过认证。
    with TestClient(
        create_app(Settings(_env_file=None), Probe(), container=FakeContainer())
    ) as client:
        for method, path in [
            ("get", "/api/v1/conversations"),
            ("post", "/api/v1/conversations"),
            ("get", "/api/v1/documents"),
            ("get", "/api/v1/data-sources"),
        ]:
            response = getattr(client, method)(path)
            assert response.status_code == 401, (method, path)
            assert response.json()["error"]["code"] == "authentication_error"


def test_health_reports_dependency_failure():
    # 依赖不可用时 readiness 返回 503，但 liveness 探针仍应为 200。
    with TestClient(
        create_app(Settings(_env_file=None), Probe("unavailable"), container=FakeContainer())
    ) as client:
        assert client.get("/api/v1/health").status_code == 503
        assert client.get("/api/v1/health/live").status_code == 200
