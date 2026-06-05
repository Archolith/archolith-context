"""Tests for the edit-fidelity probe scorer + loader in scripts/benchmark.py.

Only the pure, deterministic pieces are tested here (no network): the scorer
``score_edit_probe`` and ``Scenario.from_file`` edit-probe loading.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import benchmark  # noqa: E402


class TestScoreEditProbe:
    def test_all_required_present_full_fidelity(self) -> None:
        score = benchmark.score_edit_probe(
            "def parse(x):\n    return int(x) + 1", ["def parse", "int(x) + 1"]
        )
        assert score["fidelity"] == 1.0
        assert score["required_hits"] == 2
        assert score["forbidden_hit"] is False

    def test_partial_required_fractional_fidelity(self) -> None:
        score = benchmark.score_edit_probe("only one anchor here", ["one anchor", "missing frag"])
        assert score["required_hits"] == 1
        assert score["total_required"] == 2
        assert score["fidelity"] == 0.5

    def test_forbidden_fragment_zeros_fidelity(self) -> None:
        # All required present, but a forbidden (stale) fragment appears → 0.0
        score = benchmark.score_edit_probe(
            "new_value = 42  # old_value = 7 still referenced",
            required_fragments=["new_value = 42"],
            forbidden_fragments=["old_value = 7"],
        )
        assert score["fidelity"] == 0.0
        assert score["forbidden_hit"] is True
        assert "old_value = 7" in score["forbidden_fragments_present"]

    def test_empty_required_no_forbidden_is_full(self) -> None:
        score = benchmark.score_edit_probe("anything", [])
        assert score["fidelity"] == 1.0

    def test_none_forbidden_handled(self) -> None:
        score = benchmark.score_edit_probe("abc", ["abc"], None)
        assert score["fidelity"] == 1.0
        assert score["forbidden_hit"] is False


class TestEditProbeLoading:
    def test_scenario_loads_edit_probes(self, tmp_path: pathlib.Path) -> None:
        scenario_path = tmp_path / "s.json"
        scenario_path.write_text(
            json.dumps({
                "name": "demo",
                "description": "d",
                "system_prompt": "sp",
                "turns": ["t1"],
                "edit_probes": [{
                    "after_turn": 1,
                    "instruction": "apply the fix",
                    "required_fragments": ["foo"],
                    "forbidden_fragments": ["bar"],
                }],
            }),
            encoding="utf-8",
        )
        scenario = benchmark.Scenario.from_file(scenario_path)
        assert len(scenario.edit_probes) == 1
        probe = scenario.edit_probes[0]
        assert probe.after_turn == 1
        assert probe.required_fragments == ["foo"]
        assert probe.forbidden_fragments == ["bar"]

    def test_scenario_without_edit_probes_defaults_empty(self, tmp_path: pathlib.Path) -> None:
        scenario_path = tmp_path / "s2.json"
        scenario_path.write_text(
            json.dumps({
                "name": "demo",
                "description": "d",
                "system_prompt": "sp",
                "turns": ["t1"],
            }),
            encoding="utf-8",
        )
        scenario = benchmark.Scenario.from_file(scenario_path)
        assert scenario.edit_probes == []
