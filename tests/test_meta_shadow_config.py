"""Tests for ShadowConfig — per-target application of ProposedChange to a config deep-copy."""
from __future__ import annotations

import copy

import pytest

from src.meta.proposer import ProposedChange
from src.meta.shadow_config import META_PENDING_KEY, ShadowConfig


def _base_config():
    return {
        "GATE": {
            "cooldown_seconds": 300,
            "volume_spike_multiplier": 2.0,
            "invoke_on_bos": True,
        },
        "SIGNAL": {"min_signal_interval_seconds": 1800},
        "INDICATORS": {"ma_fast": 5, "ma_slow": 10},
    }


def _make_change(target, diff, tier="A", change_id="chg-001"):
    return ProposedChange(
        change_id=change_id,
        tier=tier,
        target=target,
        rationale="test",
        evidence=[],
        diff=diff,
        proposed_at=0,
        proposer_model="test",
        proposer_prompt_hash="deadbeef",
    )


# ------------------------------------------------------------- config_threshold


def test_config_threshold_modifies_nested_value():
    cfg = _base_config()
    change = _make_change(
        "config_threshold",
        {"path": ["GATE", "cooldown_seconds"], "before": 300, "after": 900},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert shadow["GATE"]["cooldown_seconds"] == 900
    # Base config is untouched.
    assert cfg["GATE"]["cooldown_seconds"] == 300


def test_config_threshold_deep_copies_and_records_pending():
    cfg = _base_config()
    change = _make_change(
        "config_threshold",
        {"path": ["INDICATORS", "ma_fast"], "before": 5, "after": 7},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert shadow["INDICATORS"]["ma_fast"] == 7
    pending = shadow[META_PENDING_KEY]
    assert len(pending) == 1
    assert pending[0]["change_id"] == "chg-001"
    assert pending[0]["target"] == "config_threshold"


def test_config_threshold_creates_intermediate_dicts():
    cfg = _base_config()
    change = _make_change(
        "config_threshold",
        {"path": ["NEW_SECTION", "sub", "leaf"], "after": 42},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert shadow["NEW_SECTION"]["sub"]["leaf"] == 42


def test_config_threshold_missing_path_raises():
    cfg = _base_config()
    change = _make_change("config_threshold", {"after": 900})  # no path
    with pytest.raises(ValueError, match="path"):
        ShadowConfig(cfg).apply(change)


def test_config_threshold_missing_after_raises():
    cfg = _base_config()
    change = _make_change("config_threshold", {"path": ["GATE", "cooldown_seconds"]})
    with pytest.raises(ValueError, match="after"):
        ShadowConfig(cfg).apply(change)


# ------------------------------------------------------------- validate_take_rule


def test_validate_take_rule_appends_new_clause():
    cfg = _base_config()
    change = _make_change(
        "validate_take_rule",
        {"text": "reject if session=ny-overlap AND last_bos=bear AND volume_spike"},
    )
    shadow = ShadowConfig(cfg).apply(change)
    rules = shadow["_VALIDATE_TAKE_RULES"]
    assert len(rules) == 1
    assert "ny-overlap" in rules[0]


def test_validate_take_rule_multiple_changes_stack():
    cfg = _base_config()
    sc = ShadowConfig(cfg)
    # We deep-copy under the hood, so applying twice from the SAME builder
    # yields two independent shadow dicts each with one rule.
    c1 = _make_change("validate_take_rule", {"text": "rule A"}, change_id="a")
    c2 = _make_change("validate_take_rule", {"text": "rule B"}, change_id="b")
    s1 = sc.apply(c1)
    s2 = sc.apply(c2)
    assert s1["_VALIDATE_TAKE_RULES"] == ["rule A"]
    assert s2["_VALIDATE_TAKE_RULES"] == ["rule B"]


def test_validate_take_rule_empty_text_raises():
    cfg = _base_config()
    change = _make_change("validate_take_rule", {"text": "   "})
    with pytest.raises(ValueError, match="rule body"):
        ShadowConfig(cfg).apply(change)


# ------------------------------------------------------------- lens_weight


def test_lens_weight_applies_weights_dict():
    cfg = _base_config()
    change = _make_change(
        "lens_weight",
        {"weights": {"flow": 1.2, "structure": 1.5, "context": 0.8, "intent": 0.5}},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert shadow["LENS_WEIGHTS"]["structure"] == 1.5
    assert shadow["LENS_WEIGHTS"]["intent"] == 0.5


def test_lens_weight_rejects_unknown_lens():
    cfg = _base_config()
    change = _make_change("lens_weight", {"weights": {"psychic": 2.0}})
    with pytest.raises(ValueError, match="unknown lens keys"):
        ShadowConfig(cfg).apply(change)


def test_lens_weight_rejects_negative_value():
    cfg = _base_config()
    change = _make_change("lens_weight", {"weights": {"flow": -0.5}})
    with pytest.raises(ValueError, match="non-negative"):
        ShadowConfig(cfg).apply(change)


# ------------------------------------------------------------- setup_file


def test_setup_file_stages_body_under_name():
    cfg = _base_config()
    change = _make_change(
        "setup_file",
        {"name": "opening_range_break.md", "body": "# ORB setup\n\nWhen the ..."},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert "opening_range_break.md" in shadow["_SHADOW_SETUPS"]
    assert "ORB setup" in shadow["_SHADOW_SETUPS"]["opening_range_break.md"]


def test_setup_file_missing_name_raises():
    cfg = _base_config()
    change = _make_change("setup_file", {"body": "text"})
    with pytest.raises(ValueError, match="name"):
        ShadowConfig(cfg).apply(change)


# ------------------------------------------------------------- addendum_section


def test_addendum_section_appends_text():
    cfg = _base_config()
    change = _make_change(
        "addendum_section",
        {"text": "New paragraph on liquidity sweeps."},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert shadow["_SHADOW_ADDENDUM_APPEND"] == ["New paragraph on liquidity sweeps."]


def test_addendum_section_empty_raises():
    cfg = _base_config()
    change = _make_change("addendum_section", {"text": ""})
    with pytest.raises(ValueError):
        ShadowConfig(cfg).apply(change)


# ------------------------------------------------------------- confidence_calibration


def test_confidence_calibration_installs_table():
    cfg = _base_config()
    change = _make_change(
        "confidence_calibration",
        {"table": {"0.8": [0.55, 0.65], "0.9": [0.7, 0.8]}},
    )
    shadow = ShadowConfig(cfg).apply(change)
    assert shadow["CONFIDENCE_CALIBRATION_TABLE"]["0.9"] == [0.7, 0.8]


def test_confidence_calibration_table_deep_copies():
    cfg = _base_config()
    table = {"0.5": [0.3, 0.4]}
    change = _make_change("confidence_calibration", {"table": table})
    shadow = ShadowConfig(cfg).apply(change)
    # Mutating the diff table must not mutate the shadow.
    table["0.5"].append(999)
    assert shadow["CONFIDENCE_CALIBRATION_TABLE"]["0.5"] == [0.3, 0.4]


# ------------------------------------------------------------- unsupported target


def test_unsupported_target_raises():
    cfg = _base_config()
    # Bypass the Literal by constructing a change with a bogus target via
    # dataclass field assignment (simulates a target that slipped past types).
    change = _make_change("config_threshold", {"path": ["GATE"], "after": 1})
    change.target = "proposer_prompt"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="unsupported target"):
        ShadowConfig(cfg).apply(change)


# ------------------------------------------------------------- base isolation


def test_base_config_not_mutated_by_apply():
    cfg = _base_config()
    snapshot = copy.deepcopy(cfg)
    change = _make_change(
        "config_threshold",
        {"path": ["GATE", "cooldown_seconds"], "after": 999},
    )
    ShadowConfig(cfg).apply(change)
    assert cfg == snapshot
