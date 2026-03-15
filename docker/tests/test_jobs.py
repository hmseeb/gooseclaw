"""HTTP-level tests for job CRUD and schedule endpoints."""

import json
import os
import requests


def _ensure_configured(gateway_module):
    """Ensure setup.json exists with provider so _is_first_boot() returns False."""
    gw = gateway_module
    if not os.path.exists(gw.SETUP_FILE):
        hashed = gw.hash_token("testpassword")
        setup = {
            "web_auth_token_hash": hashed,
            "setup_complete": True,
            "provider_type": "openai",
        }
        os.makedirs(os.path.dirname(gw.SETUP_FILE), exist_ok=True)
        with open(gw.SETUP_FILE, "w") as f:
            json.dump(setup, f, indent=2)


class TestJobList:
    """GET /api/jobs tests."""

    def test_list_jobs_returns_200(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.get(
            f"{live_gateway}/api/jobs",
            headers=auth_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "jobs" in data
        assert isinstance(data["jobs"], list)

    def test_list_jobs_has_count(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.get(
            f"{live_gateway}/api/jobs",
            headers=auth_session,
        )
        data = resp.json()
        assert "count" in data


class TestJobCreate:
    """POST /api/jobs tests."""

    def test_create_reminder_job(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/jobs",
            json={
                "type": "reminder",
                "text": "test reminder from pytest",
                "delay_seconds": 3600,
            },
            headers=auth_session,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data.get("created") is True
        assert "job" in data
        assert data["job"].get("id")

    def test_create_job_missing_fields(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/jobs",
            json={},
            headers=auth_session,
        )
        assert resp.status_code == 400

    def test_create_job_with_cron(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.post(
            f"{live_gateway}/api/jobs",
            json={
                "type": "reminder",
                "text": "cron test",
                "cron": "0 9 * * *",
            },
            headers=auth_session,
        )
        assert resp.status_code == 201


class TestJobDelete:
    """DELETE /api/jobs/<id> tests."""

    def test_delete_existing_job(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        # Create a job first
        create_resp = requests.post(
            f"{live_gateway}/api/jobs",
            json={
                "type": "reminder",
                "text": "to be deleted",
                "delay_seconds": 7200,
            },
            headers=auth_session,
        )
        assert create_resp.status_code == 201
        job_id = create_resp.json()["job"]["id"]

        # Delete it
        del_resp = requests.delete(
            f"{live_gateway}/api/jobs/{job_id}",
            headers=auth_session,
        )
        assert del_resp.status_code == 200
        assert del_resp.json().get("deleted") is True

        # Verify it's gone
        list_resp = requests.get(
            f"{live_gateway}/api/jobs",
            headers=auth_session,
        )
        job_ids = [j["id"] for j in list_resp.json()["jobs"]]
        assert job_id not in job_ids

    def test_delete_nonexistent_job(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.delete(
            f"{live_gateway}/api/jobs/nonexistent-id-12345",
            headers=auth_session,
        )
        assert resp.status_code == 404


class TestJobRun:
    """POST /api/jobs/<id>/run tests."""

    def test_run_job_manually(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        # Create a reminder job
        create_resp = requests.post(
            f"{live_gateway}/api/jobs",
            json={
                "type": "reminder",
                "text": "run me now",
                "delay_seconds": 7200,
            },
            headers=auth_session,
        )
        assert create_resp.status_code == 201
        job_id = create_resp.json()["job"]["id"]

        # Run it manually
        run_resp = requests.post(
            f"{live_gateway}/api/jobs/{job_id}/run",
            headers=auth_session,
        )
        # 202 Accepted is the expected response
        assert run_resp.status_code == 202
        assert run_resp.json().get("started") is True


class TestScheduleEndpoints:
    """GET /api/schedule/* tests."""

    def test_schedule_upcoming(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.get(
            f"{live_gateway}/api/schedule/upcoming",
            headers=auth_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "upcoming" in data
        assert "count" in data

    def test_schedule_context(self, live_gateway, auth_session, gateway_module):
        _ensure_configured(gateway_module)
        resp = requests.get(
            f"{live_gateway}/api/schedule/context",
            headers=auth_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "context" in data
