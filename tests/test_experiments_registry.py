"""Tests for src.experiments.registry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.experiments.registry import (
    HypothesisRegistry,
    _canonicalize,
    _similarity,
)


@pytest.fixture
def tmp_registry_path(tmp_path: Path) -> Path:
    return tmp_path / "hypotheses.parquet"


# -------------------------- canonicalization + hashing


def test_canonicalize_dict_is_sorted():
    a = {"z": 1, "a": 2, "m": {"beta": 4, "alpha": 3}}
    b = {"m": {"alpha": 3, "beta": 4}, "a": 2, "z": 1}
    assert _canonicalize(a) == _canonicalize(b)


def test_canonicalize_string_json_normalises():
    a = _canonicalize('{"b": 2, "a": 1}')
    b = _canonicalize({"a": 1, "b": 2})
    assert a == b


def test_canonicalize_bare_string_wraps():
    text = "spike-low recovery on NY open with high COT extreme"
    canonical = _canonicalize(text)
    parsed = json.loads(canonical)
    assert parsed == {"text": text}


def test_canonicalize_rejects_bad_types():
    with pytest.raises(TypeError):
        _canonicalize(42)  # type: ignore[arg-type]


# -------------------------- register / idempotency


def test_same_hypothesis_same_id(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    id_a = reg.register({"thesis": "spike-low recovery", "mechanism": "flow reversal"})
    id_b = reg.register({"mechanism": "flow reversal", "thesis": "spike-low recovery"})
    assert id_a == id_b
    assert len(reg) == 1


def test_different_hypothesis_different_id(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    id_a = reg.register({"thesis": "spike-low recovery"})
    id_b = reg.register({"thesis": "range mean reversion"})
    assert id_a != id_b
    assert len(reg) == 2


def test_ids_are_stable_across_instances(tmp_registry_path):
    hypothesis = {"thesis": "spike-low recovery", "mechanism": "flow reversal"}
    reg1 = HypothesisRegistry(tmp_registry_path)
    id_1 = reg1.register(hypothesis)

    reg2 = HypothesisRegistry(tmp_registry_path)
    id_2 = reg2.register(hypothesis)

    assert id_1 == id_2
    assert len(reg2) == 1


def test_id_format(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    hid = reg.register({"foo": "bar"})
    assert hid.startswith("hyp_")
    # 12 hex chars after prefix.
    assert len(hid) == len("hyp_") + 12


# -------------------------- record_trial + total_trials


def test_record_trial_updates_best_metrics(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    hid = reg.register({"thesis": "test"})
    reg.record_trial(hid, sharpe=1.2, pbo=0.30)
    reg.record_trial(hid, sharpe=0.9, pbo=0.15)
    reg.record_trial(hid, sharpe=1.5, pbo=0.40)

    entry = reg.get(hid)
    assert entry is not None
    assert entry.trial_count == 3
    assert entry.best_sharpe == pytest.approx(1.5)
    assert entry.best_pbo == pytest.approx(0.15)  # min PBO is best


def test_record_trial_unknown_id_raises(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    with pytest.raises(KeyError):
        reg.record_trial("hyp_deadbeef0000", sharpe=1.0, pbo=0.1)


def test_total_trials_sums_across_hypotheses(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    a = reg.register({"thesis": "one"})
    b = reg.register({"thesis": "two"})
    reg.record_trial(a, sharpe=1.0, pbo=0.10)
    reg.record_trial(a, sharpe=1.1, pbo=0.12)
    reg.record_trial(b, sharpe=0.5, pbo=0.30)
    assert reg.total_trials() == 3


# -------------------------- persistence


def test_roundtrip_persistence(tmp_registry_path):
    reg1 = HypothesisRegistry(tmp_registry_path)
    hid = reg1.register({"thesis": "spike-low recovery"})
    reg1.record_trial(hid, sharpe=2.1, pbo=0.08)
    reg1.record_trial(hid, sharpe=1.8, pbo=0.12)

    reg2 = HypothesisRegistry(tmp_registry_path)
    entry = reg2.get(hid)
    assert entry is not None
    assert entry.trial_count == 2
    assert entry.best_sharpe == pytest.approx(2.1)
    assert entry.best_pbo == pytest.approx(0.08)
    assert reg2.total_trials() == 2


def test_creates_parent_dir_if_missing(tmp_path: Path):
    nested = tmp_path / "does" / "not" / "exist" / "hyp.parquet"
    reg = HypothesisRegistry(nested)
    reg.register({"thesis": "test"})
    assert nested.exists()


# -------------------------- find_similar


def test_similarity_identical_dicts_is_one():
    a = {"thesis": "same", "mechanism": "same"}
    b = {"thesis": "same", "mechanism": "same"}
    assert _similarity(a, b) == pytest.approx(1.0)


def test_similarity_completely_different_is_low():
    a = {"thesis": "abcdefghij"}
    b = {"thesis": "zyxwvutsrq"}
    assert _similarity(a, b) < 0.3


def test_find_similar_returns_close_matches(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    reg.register(
        {"thesis": "spike-low recovery on NY open", "mechanism": "flow reversal"}
    )
    reg.register(
        {"thesis": "range mean reversion in Asia", "mechanism": "vol suppression"}
    )
    # Near-duplicate of the first entry (single-char difference in a field).
    hits = reg.find_similar(
        {"thesis": "spike-low recovery on NY open", "mechanism": "flow reversals"},
        threshold=0.85,
    )
    assert len(hits) >= 1
    # The most similar hypothesis (spike-low recovery) should come first.
    first_entry = reg.get(hits[0])
    assert first_entry is not None
    assert "spike-low" in first_entry.hypothesis_text


def test_find_similar_misses_below_threshold(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    reg.register({"thesis": "spike-low recovery on NY open"})
    # Threshold 0.95 with a very different query -> no hits.
    hits = reg.find_similar({"thesis": "totally unrelated garbage text"}, threshold=0.95)
    assert hits == []


def test_find_similar_threshold_zero_returns_all(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    reg.register({"thesis": "one"})
    reg.register({"thesis": "two"})
    reg.register({"thesis": "three"})
    hits = reg.find_similar({"thesis": "anything"}, threshold=0.0)
    assert len(hits) == 3


def test_find_similar_threshold_one_needs_exact(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    reg.register({"thesis": "spike-low"})
    reg.register({"thesis": "spike-lo"})  # very close but not exact
    hits = reg.find_similar({"thesis": "spike-low"}, threshold=1.0)
    assert len(hits) == 1


def test_find_similar_rejects_bad_threshold(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    with pytest.raises(ValueError):
        reg.find_similar({"thesis": "x"}, threshold=1.5)
    with pytest.raises(ValueError):
        reg.find_similar({"thesis": "x"}, threshold=-0.1)


# -------------------------- get()


def test_get_returns_none_for_unknown(tmp_registry_path):
    reg = HypothesisRegistry(tmp_registry_path)
    assert reg.get("hyp_nonexistent0") is None
