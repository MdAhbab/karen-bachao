#!/usr/bin/env python3
"""Live end-to-end checks against a running GridWise service.

    python tests/test_api.py                                  # localhost:8000
    python tests/test_api.py --base-url https://your.host     # deployed

Exercises the public sample pack and the synthetic scenarios, replays every
returned plan, compares cost against the organizer reference, checks the
error contract, and reports p95 latency.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.replay import replay, totals  # noqa: E402
from app.schemas import OptimizeRequest  # noqa: E402

HERE = Path(__file__).resolve().parent
TOL = 0.01
PASS, FAIL = "PASS", "FAIL"


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def check_schema(body, request_payload):
    """Response must match the section 10 contract exactly."""
    problems = []
    required = ["scenario_id", "directive_interpretation", "hourly_plan",
                "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary"]
    for field in required:
        if field not in body:
            problems.append(f"missing top-level field {field}")
    if problems:
        return problems

    if body["scenario_id"] != request_payload["scenario_id"]:
        problems.append("scenario_id was not echoed")

    notes = request_payload["operator_notes"]
    entries = body["directive_interpretation"]
    if len(entries) != len(notes):
        problems.append(f"expected {len(notes)} interpretation entries, got {len(entries)}")
    if [e.get("note_index") for e in entries] != list(range(len(entries))):
        problems.append("directive_interpretation is not in note_index order")

    allowed = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
               "no_discharge_window", "max_grid_window", "no_op"}
    for e in entries:
        kind = e.get("directive_type")
        if kind not in allowed:
            problems.append(f"unsupported directive_type {kind!r}")
        if kind == "no_op":
            if e.get("applies") is not False:
                problems.append("no_op must use applies=false")
            if e.get("structured_adjustment") is not None:
                problems.append("no_op must use a null structured_adjustment")
        else:
            if e.get("applies") is not True:
                problems.append(f"{kind} must use applies=true")
            adj = e.get("structured_adjustment")
            if not isinstance(adj, dict):
                problems.append(f"{kind} needs a structured_adjustment object")
                continue
            hours = adj.get("hours")
            if not isinstance(hours, list) or not hours:
                problems.append(f"{kind} needs a non-empty hours array")
            else:
                if hours != sorted(set(hours)):
                    problems.append(f"{kind} hours must be unique and ascending")
                if not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
                    problems.append(f"{kind} hours must be integers 0..23")
            if kind == "solar_reduction" and not 0 <= adj.get("factor", -1) <= 1:
                problems.append("solar_reduction factor must be within [0, 1]")
            if kind == "minimum_battery_reserve" and adj.get("minimum_energy_kwh") is None:
                problems.append("minimum_battery_reserve needs minimum_energy_kwh")
            if kind == "max_grid_window" and adj.get("max_grid_kwh") is None:
                problems.append("max_grid_window needs max_grid_kwh")

    plan = body["hourly_plan"]
    if len(plan) != 24 or sorted(p.get("hour") for p in plan) != list(range(24)):
        problems.append("hourly_plan must contain exactly hours 0..23")
    for p in plan:
        if p.get("battery_action") not in ("charge", "discharge", "idle"):
            problems.append(f"hour {p.get('hour')}: invalid battery_action")

    # Reported totals must be recomputable from hourly_plan (section 11.3).
    recomputed = {
        "total_grid_kwh": sum(float(p["grid_kwh"]) for p in plan),
        "peak_grid_kwh": max(float(p["grid_kwh"]) for p in plan),
    }
    for field, value in recomputed.items():
        if abs(float(body[field]) - value) > TOL:
            problems.append(f"{field} disagrees with hourly_plan")
    return problems


def run_case(client, base_url, case_id, payload, expected_directives, reference_cost):
    started = time.perf_counter()
    response = client.post(f"{base_url}/optimize-energy", json=payload, timeout=35)
    elapsed = time.perf_counter() - started

    if response.status_code != 200:
        return {"id": case_id, "status": FAIL, "elapsed": elapsed,
                "problems": [f"HTTP {response.status_code}"], "ratio": 0.0,
                "directive_hits": 0, "directive_total": len(expected_directives or [])}

    body = response.json()
    problems = check_schema(body, payload)

    request = OptimizeRequest(**payload)
    # Replay against what the service itself reported as the directives.
    problems += replay(request, body["directive_interpretation"], body["hourly_plan"])

    # Independently replay against ground truth, which is what the judge does.
    hits = 0
    total_expected = 0
    if expected_directives:
        ground_truth = []
        for i, expected in enumerate(expected_directives):
            ground_truth.append({
                "note_index": i, "applies": expected["directive_type"] != "no_op",
                "directive_type": expected["directive_type"],
                "structured_adjustment": expected.get("structured_adjustment"),
                "explanation": "",
            })
            total_expected += 1
            got = body["directive_interpretation"][i] if i < len(
                body["directive_interpretation"]) else {}
            if (got.get("directive_type") == expected["directive_type"]
                    and _adjust_matches(got.get("structured_adjustment"),
                                        expected.get("structured_adjustment"))):
                hits += 1
        problems += [f"ground truth: {p}" for p in
                     replay(request, ground_truth, body["hourly_plan"])]

    ratio = 0.0
    if not problems and reference_cost is not None:
        team_cost = totals(request, body["hourly_plan"])["total_cost_bdt"]
        if abs(reference_cost) <= TOL:
            ratio = 1.0 if abs(team_cost) <= TOL else 0.0
        else:
            ratio = min(1.0, reference_cost / team_cost) if team_cost > 0 else 1.0

    return {"id": case_id, "status": PASS if not problems else FAIL,
            "elapsed": elapsed, "problems": problems, "ratio": ratio,
            "directive_hits": hits, "directive_total": total_expected}


def _adjust_matches(got, expected):
    if expected is None:
        return got is None
    if not isinstance(got, dict):
        return False
    for key, value in expected.items():
        if key not in got:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(float(got[key]) - float(value)) > TOL:
                return False
        elif got[key] != value:
            return False
    return True


def check_error_contract(client, base_url):
    """Bad input must be rejected cleanly, never with a 5xx."""
    results = []

    r = client.post(f"{base_url}/optimize-energy",
                    content=b"{not json at all",
                    headers={"Content-Type": "application/json"}, timeout=20)
    results.append(("malformed JSON", r.status_code in (400, 422), r.status_code))

    r = client.post(f"{base_url}/optimize-energy",
                    json={"scenario_id": "X", "operator_notes": []}, timeout=20)
    results.append(("empty operator_notes", r.status_code in (400, 422), r.status_code))

    r = client.post(f"{base_url}/optimize-energy", json={"nothing": "useful"}, timeout=20)
    results.append(("missing fields", r.status_code in (400, 422), r.status_code))

    sample = load("public_cases.json")["cases"][0]["input"]
    truncated = dict(sample, hours=sample["hours"][:5])
    r = client.post(f"{base_url}/optimize-energy", json=truncated, timeout=20)
    results.append(("only 5 hours", r.status_code in (400, 422), r.status_code))

    body = r.text.lower()
    leaked = any(token in body for token in ("traceback", "gemini_api_key", "aq.ab8"))
    results.append(("no secret or stack trace leaked", not leaked, "clean" if not leaked else "LEAK"))
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    client = httpx.Client()
    failures = 0

    print(f"\nGridWise API verification against {base_url}")
    print("=" * 72)

    # --- health ---------------------------------------------------------
    started = time.perf_counter()
    try:
        health = client.get(f"{base_url}/health", timeout=15)
        ok = health.status_code == 200 and health.json().get("status") == "ok"
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  /health unreachable: {exc}")
        return 1
    print(f"  {'PASS' if ok else 'FAIL'}  GET /health -> {health.text.strip()} "
          f"({(time.perf_counter() - started) * 1000:.0f}ms)")
    failures += 0 if ok else 1

    # --- public sample pack --------------------------------------------
    print("\nPublic sample cases")
    print("-" * 72)
    latencies, ratios, hits, hit_total = [], [], 0, 0
    for case in load("public_cases.json")["cases"]:
        expected = [
            {"directive_type": e["directive_type"],
             "structured_adjustment": e["structured_adjustment"]}
            for e in case["expected_output"]["directive_interpretation"]
        ]
        result = run_case(client, base_url, case["id"], case["input"], expected,
                          case["expected_output"]["total_cost_bdt"])
        latencies.append(result["elapsed"])
        ratios.append(result["ratio"])
        hits += result["directive_hits"]
        hit_total += result["directive_total"]
        failures += 0 if result["status"] == PASS else 1
        print(f"  {result['status']}  {result['id']:<11} {result['elapsed']:5.2f}s  "
              f"quality {result['ratio']:.3f}  "
              f"directives {result['directive_hits']}/{result['directive_total']}")
        for problem in result["problems"][:4]:
            print(f"          - {problem}")

    # --- synthetic scenarios -------------------------------------------
    print("\nSynthetic scenarios (paraphrase and boundary coverage)")
    print("-" * 72)
    for scenario in load("scenarios.json")["scenarios"]:
        result = run_case(client, base_url, scenario["id"], scenario["input"],
                          scenario["expected_directives"], None)
        latencies.append(result["elapsed"])
        hits += result["directive_hits"]
        hit_total += result["directive_total"]
        failures += 0 if result["status"] == PASS else 1
        print(f"  {result['status']}  {result['id']:<11} {result['elapsed']:5.2f}s  "
              f"directives {result['directive_hits']}/{result['directive_total']}  "
              f"{scenario['label']}")
        for problem in result["problems"][:4]:
            print(f"          - {problem}")

    # --- error contract -------------------------------------------------
    print("\nError handling")
    print("-" * 72)
    for label, ok, detail in check_error_contract(client, base_url):
        failures += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<34} {detail}")

    # --- summary --------------------------------------------------------
    latencies.sort()
    p95 = latencies[max(0, int(len(latencies) * 0.95) - 1)]
    print("\n" + "=" * 72)
    print(f"  cases run        : {len(latencies)}")
    print(f"  failures         : {failures}")
    print(f"  directive match  : {hits}/{hit_total}"
          f"  ({100 * hits / hit_total:.0f}%)" if hit_total else "")
    print(f"  optimization     : {sum(ratios) / len(ratios):.4f} average quality ratio"
          if ratios else "")
    print(f"  latency          : p95 {p95:.2f}s   max {latencies[-1]:.2f}s")
    print("=" * 72 + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
