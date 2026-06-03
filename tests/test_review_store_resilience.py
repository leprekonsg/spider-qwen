"""ReviewStore resilience: a corrupt persisted store must not crash a run, and
writes are atomic (no half-written or temp file is ever left behind).

Regression guard for the failure seen when many offline runs shared the default
state dir and interleaved writes into one 16 MB invalid JSON file that then
crashed `_read` for every later run.
"""

from __future__ import annotations

import json

from spider_qwen.governance.review_events import ReviewStore


def test_corrupt_store_is_quarantined_not_fatal(tmp_path):
    store = ReviewStore(tmp_path)
    store.create(run_id="r1", reason="needs review", proposed_action="rfq_draft_review")
    path = tmp_path / "reviews" / "pending_human_review.json"

    path.write_text("{ this is not valid json", encoding="utf-8")  # simulate an interleaved write

    # _read tolerates corruption: no crash, empty result, bad file quarantined.
    assert store.list() == []
    assert (tmp_path / "reviews" / "pending_human_review.corrupt").exists()

    # the store self-heals -- the next write produces a valid, readable store.
    event = store.create(run_id="r2", reason="later", proposed_action="rfq_draft_review")
    assert [e.event_id for e in store.list()] == [event.event_id]
    assert json.loads(path.read_text(encoding="utf-8"))  # valid JSON again


def test_write_leaves_no_temp_files(tmp_path):
    store = ReviewStore(tmp_path)
    store.create(run_id="r1", reason="needs review", proposed_action="rfq_draft_review")
    assert list((tmp_path / "reviews").glob("*.tmp")) == []
