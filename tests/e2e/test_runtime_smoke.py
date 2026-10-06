import os

import httpx
import pytest


@pytest.mark.smoke
def test_live_runtime():
    url = os.getenv("SMOKE_BASE_URL")
    if not url:
        pytest.skip("Set SMOKE_BASE_URL to the running frontend URL for live acceptance.")
    with httpx.Client(base_url=url, timeout=15) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Enterprise Multi-Source AI Agent" in page.text
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["services"] == {
            "backend": "healthy",
            "mysql": "healthy",
            "milvus": "healthy",
        }
