"""Tests for src.experiments.logger."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.experiments.logger import Experiment, ExperimentLogger


@pytest.fixture
def logger(tmp_path: Path) -> ExperimentLogger:
    return ExperimentLogger(root=tmp_path / "log")


# -------------------------- start()


def test_start_returns_stable_id_and_persists_stub(logger):
    exp_id = logger.start(
        hypothesis_id="hyp_abc123",
        config={"cost_bp": 11, "bar_seconds": 300},
    )
    assert exp_id.startswith("exp_")
    on_disk = logger.load(exp_id)
    assert on_disk.hypothesis_id == "hyp_abc123"
    assert on_disk.config_snapshot == {"cost_bp": 11, "bar_seconds": 300}
    assert on_disk.finished_at is None
    assert on_disk.result is None
    assert on_disk.events == []


def test_start_ids_are_unique(logger):
    a = logger.start("hyp_a", {})
    b = logger.start("hyp_b", {})
    assert a != b


# -------------------------- log()


def test_log_appends_events(logger):
    exp_id = logger.start("hyp_a", {})
    logger.log(exp_id, "fold_done", {"fold": 0, "sharpe": 1.4})
    logger.log(exp_id, "fold_done", {"fold": 1, "sharpe": 1.1})
    exp = logger.load(exp_id)
    assert len(exp.events) == 2
    assert exp.events[0]["key"] == "fold_done"
    assert exp.events[0]["value"] == {"fold": 0, "sharpe": 1.4}
    assert "ts" in exp.events[0]


def test_log_unknown_id_raises(logger):
    with pytest.raises(FileNotFoundError):
        logger.log("exp_does_not_exist", "x", 1)


# -------------------------- finish()


def test_finish_records_result_and_finished_at(logger):
    exp_id = logger.start("hyp_a", {})
    verdict = {
        "hypothesis_id": "hyp_a",
        "verdict": "PASS",
        "pbo": 0.12,
        "dsr": 0.98,
        "aggregated_oos_trades": 240,
    }
    logger.finish(exp_id, verdict)

    exp = logger.load(exp_id)
    assert exp.finished_at is not None
    assert isinstance(exp.finished_at, pd.Timestamp)
    assert exp.result == verdict


# -------------------------- query()


def test_query_filters_by_hypothesis_id(logger):
    a = logger.start("hyp_a", {"tag": "alpha"})
    b = logger.start("hyp_b", {"tag": "beta"})
    logger.finish(a, {"verdict": "PASS"})
    logger.finish(b, {"verdict": "FAIL"})

    hits = logger.query({"hypothesis_id": "hyp_a"})
    ids = [e.experiment_id for e in hits]
    assert ids == [a]


def test_query_filters_by_result_field(logger):
    a = logger.start("hyp_a", {})
    b = logger.start("hyp_b", {})
    c = logger.start("hyp_c", {})
    logger.finish(a, {"verdict": "PASS"})
    logger.finish(b, {"verdict": "FAIL"})
    logger.finish(c, {"verdict": "PASS"})

    hits = logger.query({"verdict": "PASS"})
    ids = sorted(e.experiment_id for e in hits)
    assert ids == sorted([a, c])


def test_query_no_filter_returns_all(logger):
    ids = {logger.start(f"hyp_{i}", {}) for i in range(3)}
    hits = logger.query()
    assert {e.experiment_id for e in hits} == ids


def test_query_by_config_field(logger):
    a = logger.start("hyp_a", {"cost_bp": 11})
    logger.start("hyp_b", {"cost_bp": 5})
    hits = logger.query({"cost_bp": 11})
    assert [e.experiment_id for e in hits] == [a]


# -------------------------- persistence across instances


def test_persistence_across_instance_recreation(tmp_path: Path):
    root = tmp_path / "log"
    logger1 = ExperimentLogger(root=root)
    exp_id = logger1.start("hyp_a", {"foo": "bar"})
    logger1.log(exp_id, "event", 42)
    logger1.finish(exp_id, {"verdict": "PASS"})

    logger2 = ExperimentLogger(root=root)
    exp = logger2.load(exp_id)
    assert exp.experiment_id == exp_id
    assert exp.hypothesis_id == "hyp_a"
    assert exp.config_snapshot == {"foo": "bar"}
    assert exp.result == {"verdict": "PASS"}
    assert len(exp.events) == 1
    assert exp.events[0]["value"] == 42


def test_query_finds_experiments_after_recreation(tmp_path: Path):
    root = tmp_path / "log"
    logger1 = ExperimentLogger(root=root)
    a = logger1.start("hyp_a", {})
    logger1.finish(a, {"verdict": "PASS"})

    logger2 = ExperimentLogger(root=root)
    hits = logger2.query({"verdict": "PASS"})
    assert [e.experiment_id for e in hits] == [a]


# -------------------------- load()


def test_load_unknown_raises(logger):
    with pytest.raises(FileNotFoundError):
        logger.load("exp_does_not_exist")


# -------------------------- disk layout


def test_one_file_per_experiment(tmp_path: Path):
    root = tmp_path / "log"
    logger = ExperimentLogger(root=root)
    ids = [logger.start(f"hyp_{i}", {}) for i in range(3)]
    files = sorted(root.glob("*.json"))
    assert len(files) == 3
    for exp_id in ids:
        assert (root / f"{exp_id}.json").exists()


def test_experiment_json_is_valid(tmp_path: Path):
    root = tmp_path / "log"
    logger = ExperimentLogger(root=root)
    exp_id = logger.start("hyp_a", {"n": 1})
    on_disk = json.loads((root / f"{exp_id}.json").read_text())
    assert on_disk["experiment_id"] == exp_id
    assert on_disk["hypothesis_id"] == "hyp_a"
    assert on_disk["config_snapshot"] == {"n": 1}
