"""Tests for the cronjob tool schema shape.

Guards the description text that flags ``schedule`` (and ``prompt``) as
REQUIRED for ``action=create`` — the load-bearing fix for description-driven
models (e.g. Grok) that omit schedule when the schema only lists ``action``
in ``required[]``. See issue #32427 / PR #32448.
"""

from __future__ import annotations


def test_cronjob_schema_action_description_flags_create_requirements():
    """`action` description must state schedule + prompt are required for create."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    action_desc = CRONJOB_SCHEMA["parameters"]["properties"]["action"]["description"]
    assert "action=create" in action_desc
    assert "schedule" in action_desc
    assert "REQUIRED" in action_desc


def test_cronjob_schema_distinguishes_one_shot_duration_from_recurring_interval():
    """Models must not turn a requested watchdog into a one-shot by mistake."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    schedule_desc = CRONJOB_SCHEMA["parameters"]["properties"]["schedule"]["description"]
    assert "'30m' (one-shot" in schedule_desc
    assert "'every 30m' (recurring" in schedule_desc


