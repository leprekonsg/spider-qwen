"""Guardrail #3.2 (no-action invariant): the AuditLog must reject any action that
would submit/send an RFQ or drive a browser. Search + fetch is the whole v1
surface; nothing else may be recorded as having happened.
"""

from __future__ import annotations

import pytest

from spider_qwen.governance.audit import AuditLog, PolicyViolation

# Every spelling the spec (BUILD_PLAN section 3) and the hard rules name.
FORBIDDEN_ACTIONS = [
    "rfq_submitted", "rfq_sent",
    "form_submit", "form_submitted",
    "email_send", "email_sent",
    "browser_drive", "browser_navigate", "browser_action",
]


@pytest.mark.parametrize("action", FORBIDDEN_ACTIONS)
def test_forbidden_action_raises(action: str):
    log = AuditLog(run_id="run_test")
    with pytest.raises(PolicyViolation):
        log.record(action)
    assert log.events == [], "a forbidden action must not be recorded"


def test_allowed_action_is_recorded():
    log = AuditLog(run_id="run_test")
    event = log.record("rfq_draft_generated", vendor="Acme", status="complete")
    assert event.action == "rfq_draft_generated"
    assert log.events == [event]


def test_browser_drive_is_blocked():
    """Regression: the browser/agent automation forbidden by 'search + fetch only'
    was previously absent from FORBIDDEN, so this token would NOT have raised."""
    log = AuditLog(run_id="run_test")
    with pytest.raises(PolicyViolation):
        log.record("browser_drive")
