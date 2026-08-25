"""Regression tests for cronjob list execution truth."""

import json
from unittest.mock import patch

from tools.cronjob_tools import cronjob


def test_list_includes_latest_durable_execution_state():
    execution = {
        "id": "exec-live",
        "job_id": "job-running",
        "source": "direct",
        "status": "running",
        "claimed_at": "2026-08-25T12:00:00-07:00",
        "started_at": "2026-08-25T12:00:01-07:00",
        "finished_at": None,
        "error": None,
    }
    job = {
        "id": "job-running",
        "name": "running job",
        "prompt": "work",
        "schedule": {"kind": "interval", "minutes": 30},
        "schedule_display": "every 30m",
        "enabled": True,
        "latest_execution": execution,
    }

    with patch("tools.cronjob_tools.list_jobs", return_value=[job]):
        payload = json.loads(cronjob(action="list"))

    assert payload["jobs"][0]["execution"] == {
        "id": "exec-live",
        "source": "direct",
        "status": "running",
        "claimed_at": "2026-08-25T12:00:00-07:00",
        "started_at": "2026-08-25T12:00:01-07:00",
        "finished_at": None,
        "error": None,
    }
