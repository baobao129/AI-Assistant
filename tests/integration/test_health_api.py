from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.infrastructure.config import Settings


class Probe:
    def __init__(self, status="healthy"):
        self.status = status

    def check(self):
        return {"mysql": self.status, "milvus": self.status}


def test_health_and_correlation():
    with TestClient(create_app(Settings(_env_file=None), Probe())) as client:
        response = client.get("/api/v1/health", headers={"X-Request-ID": "test-request"})
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.headers["X-Request-ID"] == "test-request"
        missing = client.get("/api/v1/runs/unknown")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        assert set(missing.json()["error"]) == {"code", "message", "request_id", "details"}


def test_health_reports_dependency_failure():
    with TestClient(create_app(Settings(_env_file=None), Probe("unavailable"))) as client:
        assert client.get("/api/v1/health").status_code == 503
        assert client.get("/api/v1/health/live").status_code == 200
