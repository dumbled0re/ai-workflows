from datetime import UTC, datetime

import pytest

from point_sites.common.models import ClickCandidate, ClickResult
from point_sites.common.state_store import StateStore


def _candidate(url: str = "https://pc.moppy.jp/redirect/abc?uid=1") -> ClickCandidate:
    return ClickCandidate(
        url=url,  # type: ignore[arg-type]
        anchor_text="クリックで1pt",
        estimated_points=1,
        extraction_reason="whitelist_url_pattern_and_anchor",
    )


def _result(candidate: ClickCandidate, status: str = "success") -> ClickResult:
    return ClickResult(
        candidate=candidate,
        final_status=status,  # type: ignore[arg-type]
        http_status=200 if status == "success" else 500,
        final_host="pc.moppy.jp",
        duration_ms=42,
        timestamp=datetime.now(UTC),
    )


def test_record_and_query_success(tmp_path):
    store = StateStore(tmp_path / "state.json")
    candidate = _candidate()
    store.increment_attempt("msg1")
    store.record_attempt("msg1", _result(candidate))
    store.save()

    reloaded = StateStore(tmp_path / "state.json")
    assert reloaded.is_url_done("msg1", str(candidate.url), max_attempts=3)
    assert reloaded.is_message_complete("msg1", max_attempts=3)


def test_failed_url_eligible_until_max_attempts(tmp_path):
    store = StateStore(tmp_path / "state.json")
    candidate = _candidate()

    for _ in range(2):
        store.increment_attempt("msg2")
        store.record_attempt("msg2", _result(candidate, status="failed_5xx"))
    assert not store.is_url_done("msg2", str(candidate.url), max_attempts=3)

    store.increment_attempt("msg2")
    store.record_attempt("msg2", _result(candidate, status="failed_5xx"))
    assert store.is_url_done("msg2", str(candidate.url), max_attempts=3)
    assert store.is_message_complete("msg2", max_attempts=3)


def test_corrupt_state_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not valid json", encoding="utf-8")
    store = StateStore(path)
    assert store.attempt_count("anything") == 0
    assert path.with_suffix(".json.corrupt").exists()


def test_atomic_save(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.increment_attempt("msg3")
    store.save()
    assert (tmp_path / "state.json").exists()
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_prune_old(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.json")
    store.increment_attempt("old")
    msg = store._state.messages["old"]
    msg.last_attempt = datetime(2020, 1, 1, tzinfo=UTC)
    removed = store.prune_old(days=30)
    assert removed == 1
    assert "old" not in store._state.messages


def test_unknown_message_not_complete(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert not store.is_message_complete("never-seen", max_attempts=3)


def test_redacted_url_stored(tmp_path):
    store = StateStore(tmp_path / "state.json")
    candidate = _candidate("https://pc.moppy.jp/redirect/abc?uid=secret&token=leak")
    store.increment_attempt("msg4")
    store.record_attempt("msg4", _result(candidate))
    raw = (tmp_path / "state.json").read_text() if False else store._state.model_dump_json()
    assert "secret" not in raw
    assert "leak" not in raw


@pytest.mark.parametrize("attempts", [1, 2, 3])
def test_attempt_count_tracked(tmp_path, attempts):
    store = StateStore(tmp_path / "state.json")
    for _ in range(attempts):
        store.increment_attempt("msg5")
    assert store.attempt_count("msg5") == attempts
