from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def score(scenario: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    analysis = result.get("final_analysis") or {}
    text = json.dumps(result).lower()
    expected = [str(item).lower() for item in scenario.get("expected_findings", [])]
    forbidden = [str(item).lower() for item in scenario.get("forbidden_conclusions", [])]
    expected_agents = set(scenario.get("expected_external_agents", []))
    actual_agents = {item.get("type") for item in result.get("external_findings", [])}

    found = [item for item in expected if item.replace("_", " ") in text or item in text]
    violations = [item for item in forbidden if item.replace("_", " ") in json.dumps(analysis).lower() or item in json.dumps(analysis).lower()]
    root_category = scenario["root_cause"]["category"].lower()
    root_match = root_category in text or root_category.replace("_", " ") in text
    external_match = expected_agents == actual_agents

    points = 30 * int(root_match)
    points += 20 * (len(found) / max(1, len(expected)))
    points += 10 * int(external_match)
    points += 15 * int(not violations)
    query_count = int(result.get("query_count", 99))
    iteration_count = int(result.get("iteration", 99))
    points += 10 if query_count <= 8 and iteration_count <= 8 else 5 if query_count <= 12 else 0
    points += 15 if len(result.get("investigation_history", [])) >= 2 else 0
    return {
        "score": round(points, 1),
        "root_cause_match": root_match,
        "expected_findings_found": found,
        "external_routing_match": external_match,
        "forbidden_conclusions": violations,
        "query_count": query_count,
        "iteration_count": iteration_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    with args.scenario.open() as scenario_file, args.result.open() as result_file:
        print(json.dumps(score(yaml.safe_load(scenario_file), json.load(result_file)), indent=2))


if __name__ == "__main__":
    main()
