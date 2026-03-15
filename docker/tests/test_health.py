"""Smoke tests for health endpoints — proves live_gateway fixture works end-to-end."""

import requests


class TestHealth:
    """GET /api/health returns expected response."""

    def test_health_returns_200(self, live_gateway):
        resp = requests.get(f"{live_gateway}/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == "gooseclaw"

    def test_health_ready_returns_503_when_not_configured(self, live_gateway):
        """No goosed running in test env, so readiness probe returns 503."""
        resp = requests.get(f"{live_gateway}/api/health/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["ready"] is False

    def test_health_jobs_returns_200(self, live_gateway):
        """Job engine health endpoint returns JSON."""
        resp = requests.get(f"{live_gateway}/api/health/jobs")
        # May be 200 or 503 depending on engine state, but should return valid JSON
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "healthy" in data

    def test_health_includes_security_headers(self, live_gateway):
        """Health endpoint includes X-Content-Type-Options: nosniff."""
        resp = requests.get(f"{live_gateway}/api/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
