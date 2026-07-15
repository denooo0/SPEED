"""Shadow-config application for meta-loop proposed changes.

The oracle needs to test a `ProposedChange` without touching live state.
`ShadowConfig` produces a deep-copy of the current runtime config with
the change applied, so both baseline and shadow can be handed to the
backtest harness via distinct hypothesis dicts.

Per `ChangeTarget` in `src/meta/proposer.py`:

    config_threshold        → mutate a value inside the config dict
    validate_take_rule      → append a new rejection clause (data-side only)
    lens_weight             → replace / patch the confidence-lens weights
    setup_file              → stage a new memory/setups/<name>.md file
    addendum_section        → append a paragraph to the addendum
    confidence_calibration  → install / update a Platt-scaling table

Targets not on the `ChangeTarget` Literal (proposer prompt, mandate,
risk formula …) are Tier-D forbidden by the type system and raise here
if they somehow arrive.

Diff schema (canonical form, ProposedChange.diff):

    {"path": [<config_key>, ...], "before": <old>, "after": <new>}

Alternative accepted forms (for non-config targets):

    {"text": "..."}                                      # addendum / rule / setup
    {"name": "...", "body": "..."}                       # setup_file
    {"table": {bucket: (a, b), ...}}                      # confidence_calibration
    {"weights": {"flow": ..., "structure": ..., "context": ..., "intent": ...}}

Applied changes are added to a pending record under the reserved
`_META_PENDING` key so downstream code (backtest strategy factory,
audit log) can inspect what was applied without executing anything
against live systems.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List

from src.meta.proposer import ProposedChange


META_PENDING_KEY = "_META_PENDING"


class ShadowConfig:
    """Apply a ProposedChange to a deep-copy of the current config.

    The result is a NEW dict; the base_config is never mutated in place.
    Callers pass the shadow dict as a hypothesis into the backtest harness.
    """

    def __init__(self, base_config: Dict[str, Any]) -> None:
        if not isinstance(base_config, dict):
            raise TypeError("base_config must be a dict")
        self.base_config = base_config

    # ------------------------------------------------------------ public

    def apply(self, change: ProposedChange) -> Dict[str, Any]:
        shadow = copy.deepcopy(self.base_config)
        pending = shadow.setdefault(META_PENDING_KEY, [])
        target = change.target
        diff = change.diff or {}

        if target == "config_threshold":
            self._apply_config_threshold(shadow, diff)
        elif target == "validate_take_rule":
            self._apply_validate_take_rule(shadow, diff, pending)
        elif target == "lens_weight":
            self._apply_lens_weight(shadow, diff)
        elif target == "setup_file":
            self._apply_setup_file(shadow, diff, pending)
        elif target == "addendum_section":
            self._apply_addendum_section(shadow, diff, pending)
        elif target == "confidence_calibration":
            self._apply_confidence_calibration(shadow, diff)
        else:
            # Tier-D-shaped target that slipped past the type system.
            raise ValueError(
                f"ShadowConfig cannot apply unsupported target: {target!r}"
            )

        pending.append(
            {
                "change_id": change.change_id,
                "tier": change.tier,
                "target": target,
                "diff": diff,
            }
        )
        return shadow

    # ------------------------------------------------------------ handlers

    @staticmethod
    def _resolve_path(cfg: Dict[str, Any], path: List[str]) -> Dict[str, Any]:
        """Walk `cfg` down `path[:-1]`, creating intermediate dicts as needed.

        Returns the leaf-parent dict; caller assigns `path[-1]` on it.
        """
        node = cfg
        for key in path[:-1]:
            if not isinstance(node.get(key), dict):
                node[key] = {}
            node = node[key]
        return node

    def _apply_config_threshold(self, cfg: Dict[str, Any], diff: Dict[str, Any]) -> None:
        path = diff.get("path")
        if not isinstance(path, list) or not path:
            raise ValueError(
                "config_threshold diff requires non-empty 'path' list"
            )
        if "after" not in diff:
            raise ValueError("config_threshold diff requires 'after' value")
        parent = self._resolve_path(cfg, path)
        parent[path[-1]] = diff["after"]

    def _apply_validate_take_rule(
        self,
        cfg: Dict[str, Any],
        diff: Dict[str, Any],
        pending: List[Dict[str, Any]],
    ) -> None:
        # New rejection clauses are additive-only (Tier C anti-loosening intent).
        text = diff.get("text") or diff.get("after")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                "validate_take_rule diff requires 'text' (or 'after') rule body"
            )
        rules = cfg.setdefault("_VALIDATE_TAKE_RULES", [])
        rules.append(text.strip())

    def _apply_lens_weight(self, cfg: Dict[str, Any], diff: Dict[str, Any]) -> None:
        weights = diff.get("weights") or diff.get("after")
        if not isinstance(weights, dict):
            raise ValueError(
                "lens_weight diff requires 'weights' dict with lens keys"
            )
        allowed = {"flow", "structure", "context", "intent"}
        bad = set(weights) - allowed
        if bad:
            raise ValueError(f"lens_weight has unknown lens keys: {sorted(bad)}")
        for v in weights.values():
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(
                    "lens_weight values must be non-negative numeric"
                )
        cfg.setdefault("LENS_WEIGHTS", {}).update(weights)

    def _apply_setup_file(
        self,
        cfg: Dict[str, Any],
        diff: Dict[str, Any],
        pending: List[Dict[str, Any]],
    ) -> None:
        name = diff.get("name") or diff.get("filename")
        body = diff.get("body") or diff.get("after") or diff.get("text")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("setup_file diff requires 'name'")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("setup_file diff requires 'body'")
        setups = cfg.setdefault("_SHADOW_SETUPS", {})
        setups[name.strip()] = body

    def _apply_addendum_section(
        self,
        cfg: Dict[str, Any],
        diff: Dict[str, Any],
        pending: List[Dict[str, Any]],
    ) -> None:
        text = diff.get("text") or diff.get("after")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                "addendum_section diff requires non-empty 'text'"
            )
        cfg.setdefault("_SHADOW_ADDENDUM_APPEND", []).append(text)

    def _apply_confidence_calibration(
        self,
        cfg: Dict[str, Any],
        diff: Dict[str, Any],
    ) -> None:
        table = diff.get("table") or diff.get("after")
        if not isinstance(table, dict):
            raise ValueError(
                "confidence_calibration diff requires 'table' dict"
            )
        # Copy defensively to avoid diff-mutation surprises.
        cfg["CONFIDENCE_CALIBRATION_TABLE"] = copy.deepcopy(table)
