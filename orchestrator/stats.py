"""Aggregate analysis over the run store (Tier 1 #4, third half).

`--show-run` answers "what happened in that run". This module answers the
questions that only appear once you have a hundred of them, and that nobody
else's tooling can answer about *your* agents on *your* tasks:

* Which agent / model / role pairings actually pass verification?
* How often does a verifier say PASS while the project's own test command is red?
* What does a pass cost in tokens, against what a failure costs?
* Does repair attempt #2 ever succeed when #1 did not - is the ladder earning
  its escalation, or just spending?
* Do verifier ensembles change outcomes enough to justify their price?

Those are the questions behind the configuration knobs the orchestrator now has
(consensus policy, ladder depth, ensemble size), and until now they were set on
taste. The run store is the dataset that settles them.

Load-bearing rules
------------------
* **Read-only and total.** Reads runs, changes nothing, and never raises on a
  malformed or partially-written run: an unreadable run is skipped and counted.
* **Never impute.** Token counts come only from executions that reported them;
  runs whose usage is unavailable are excluded from cost statistics and that
  exclusion is reported, so a small sample cannot masquerade as a finding.
* **Say the sample size.** Every rate is reported with its denominator. A 100%
  pass rate over two runs is not a fact about a model.
"""

from typing import Any, Dict, List, Optional, Tuple

from orchestrator.store import (
    EVENT_ACCEPTANCE,
    EVENT_AGENT_RESULT,
    EVENT_VERIFICATION,
    list_runs,
    load_run,
)
from orchestrator.status import is_successful

#: Below this many observations, a rate is shown but flagged as thin.
THIN_SAMPLE = 5


def _pct(numerator: int, denominator: int) -> Optional[float]:
    """Return a percentage, or None when there is nothing to divide by."""
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def _mean(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _usage_total(result: Dict[str, Any]) -> Optional[int]:
    """Return the tokens an execution reported, or None when it reported none."""
    usage = result.get("token_usage") or {}
    if usage.get("available") is not True:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    if isinstance(inp, int) and isinstance(out, int):
        return inp + out
    return None


def collect_runs(
    project_root: str,
    limit: int = 0,
    directory: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Load full run records, newest first.

    Returns:
        ``(runs, unreadable_count)``. A run whose directory exists but whose log
        cannot be read is counted rather than silently dropped.
    """
    runs: List[Dict[str, Any]] = []
    unreadable = 0
    for entry in list_runs(project_root, limit=limit, directory=directory):
        run_id = entry.get("run_id")
        if not run_id:
            unreadable += 1
            continue
        full = load_run(project_root, str(run_id), directory=directory)
        if not full:
            unreadable += 1
            continue
        runs.append(full)
    return runs, unreadable


def _events(run: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    return [e for e in (run.get("events") or []) if e.get("event") == name]


def _run_results(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(e.get("result") or {}) for e in _events(run, EVENT_AGENT_RESULT)]


def _run_verifications(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(e.get("record") or {}) for e in _events(run, EVENT_VERIFICATION)]


def _run_gates(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [dict(e.get("check") or {}) for e in _events(run, EVENT_ACCEPTANCE)]


def _run_tokens(results: List[Dict[str, Any]]) -> Tuple[int, bool]:
    """Return a run's total reported tokens and whether every execution reported."""
    total = 0
    complete = bool(results)
    for result in results:
        value = _usage_total(result)
        if value is None:
            complete = False
            continue
        total += value
    return total, complete


def compute_stats(runs: List[Dict[str, Any]], unreadable: int = 0) -> Dict[str, Any]:
    """Aggregate a list of full run records into the report `--stats` prints.

    Every section carries its own denominator, because the interesting question
    is almost always "over how many runs?".
    """
    report: Dict[str, Any] = {
        "runs_total": len(runs),
        "runs_unreadable": unreadable,
        "by_status": {},
        "outcomes": {},
        "cost": {},
        "pairings": [],
        "repair": {},
        "gate": {},
        "ensembles": {},
        "settings": {},
    }
    if not runs:
        return report

    # -- outcomes ----------------------------------------------------------
    finished: List[Dict[str, Any]] = []
    for run in runs:
        status = str(run.get("status") or "unknown")
        report["by_status"][status] = report["by_status"].get(status, 0) + 1
        if status not in ("running", "unknown", "unreadable"):
            finished.append(run)

    passed = [r for r in finished if str(r.get("verdict") or "") == "PASS"]
    report["outcomes"] = {
        "finished": len(finished),
        "passed": len(passed),
        "pass_rate": _pct(len(passed), len(finished)),
        "blocked": sum(1 for r in finished if str(r.get("status")) == "blocked"),
        "budget_exhausted": sum(
            1 for r in finished if str(r.get("status")) == "budget_exhausted"
        ),
        "errored": sum(1 for r in finished if str(r.get("status")) == "error"),
    }

    # -- cost of a pass versus a failure -----------------------------------
    pass_costs: List[float] = []
    fail_costs: List[float] = []
    incomplete_usage = 0
    for run in finished:
        results = _run_results(run)
        total, complete = _run_tokens(results)
        if not complete:
            incomplete_usage += 1
            continue
        succeeded = is_successful(str(run.get("status") or "")) and str(run.get("verdict")) == "PASS"
        (pass_costs if succeeded else fail_costs).append(float(total))

    report["cost"] = {
        "runs_with_complete_usage": len(pass_costs) + len(fail_costs),
        "runs_with_incomplete_usage": incomplete_usage,
        "mean_tokens_pass": _mean(pass_costs),
        "mean_tokens_fail": _mean(fail_costs),
        "passes_measured": len(pass_costs),
        "failures_measured": len(fail_costs),
    }

    # -- agent / model / role pairings --------------------------------------
    pairings: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for run in runs:
        for result in _run_results(run):
            key = (
                str(result.get("agent") or "?"),
                str(result.get("model") or "-"),
                str(result.get("role") or "?"),
            )
            row = pairings.setdefault(
                key,
                {
                    "agent": key[0],
                    "model": key[1],
                    "role": key[2],
                    "executions": 0,
                    "errors": 0,
                    "verdicts": 0,
                    "passes": 0,
                    "tokens": [],
                    "durations": [],
                },
            )
            row["executions"] += 1
            if result.get("status") == "error":
                row["errors"] += 1
            verdict = result.get("verdict")
            if verdict:
                row["verdicts"] += 1
                if str(verdict) == "PASS":
                    row["passes"] += 1
            tokens = _usage_total(result)
            if tokens is not None:
                row["tokens"].append(float(tokens))
            duration = result.get("duration_seconds")
            if isinstance(duration, (int, float)):
                row["durations"].append(float(duration))

    rows: List[Dict[str, Any]] = []
    for row in pairings.values():
        rows.append(
            {
                "agent": row["agent"],
                "model": row["model"],
                "role": row["role"],
                "executions": row["executions"],
                "error_rate": _pct(row["errors"], row["executions"]),
                "verdicts": row["verdicts"],
                "pass_rate": _pct(row["passes"], row["verdicts"]),
                "mean_tokens": _mean(row["tokens"]),
                "mean_seconds": _mean(row["durations"]),
                "thin": row["executions"] < THIN_SAMPLE,
            }
        )
    rows.sort(key=lambda r: (-r["executions"], r["agent"], r["role"]))
    report["pairings"] = rows

    # -- does repair pay off? ------------------------------------------------
    # A repair "succeeded" when the verification recorded after it passed. This
    # is the question the escalation ladder is a bet on.
    attempts: Dict[int, Dict[str, int]] = {}
    runs_with_repair = 0
    for run in runs:
        records = _run_verifications(run)
        if not records:
            continue
        by_generation: Dict[int, str] = {}
        for record in records:
            generation = int(record.get("repair_attempts") or 0)
            by_generation[generation] = str(record.get("verdict") or "UNKNOWN")
        if max(by_generation) > 0:
            runs_with_repair += 1
        for generation, verdict in by_generation.items():
            if generation == 0:
                continue
            row = attempts.setdefault(generation, {"attempted": 0, "passed": 0})
            row["attempted"] += 1
            if verdict == "PASS":
                row["passed"] += 1

    report["repair"] = {
        "runs_with_at_least_one_repair": runs_with_repair,
        "by_attempt": [
            {
                "attempt": generation,
                "attempted": row["attempted"],
                "passed": row["passed"],
                "pass_rate": _pct(row["passed"], row["attempted"]),
                "thin": row["attempted"] < THIN_SAMPLE,
            }
            for generation, row in sorted(attempts.items())
        ],
    }

    # -- does the verifier agree with the objective check? -------------------
    # This is the question the acceptance gate exists to answer. A verifier that says PASS
    # over a red suite is not a slightly weaker verifier; it is a verifier whose output
    # cannot be used, and no consensus policy over several of them fixes that.
    honesty: Dict[str, Dict[str, int]] = {}
    gated_runs = 0
    for run in runs:
        gates = {int(g.get("repair_attempts") or 0): g for g in _run_gates(run) if not g.get("skipped")}
        if not gates:
            continue
        gated_runs += 1
        for record in _run_verifications(run):
            generation = int(record.get("repair_attempts") or 0)
            gate = gates.get(generation)
            if gate is None:
                continue
            verdict = str(record.get("verdict") or "UNKNOWN")
            key = str(record.get("model") or record.get("agent") or "?")
            row = honesty.setdefault(key, {"judged": 0, "pass_over_red": 0, "fail_over_green": 0})
            row["judged"] += 1
            if verdict == "PASS" and not gate.get("ok"):
                row["pass_over_red"] += 1
            elif verdict == "FAIL" and gate.get("ok"):
                row["fail_over_green"] += 1

    report["gate"] = {
        "runs_with_a_gate": gated_runs,
        "by_verifier": [
            {
                "verifier": key,
                "judged": row["judged"],
                "pass_over_red": row["pass_over_red"],
                "pass_over_red_rate": _pct(row["pass_over_red"], row["judged"]),
                "fail_over_green": row["fail_over_green"],
                "thin": row["judged"] < THIN_SAMPLE,
            }
            for key, row in sorted(honesty.items())
        ],
    }

    # -- do ensembles change outcomes enough to justify their cost? ----------
    buckets: Dict[str, Dict[str, Any]] = {}
    for run in finished:
        verifier_count = len(
            {
                (str(r.get("agent")), str(r.get("model")))
                for r in _run_results(run)
                if r.get("role") == "verifier"
            }
        )
        bucket_name = "single verifier" if verifier_count <= 1 else f"{verifier_count} verifiers"
        bucket = buckets.setdefault(
            bucket_name,
            {"runs": 0, "passed": 0, "tokens": [], "verifier_count": verifier_count},
        )
        bucket["runs"] += 1
        if str(run.get("verdict")) == "PASS":
            bucket["passed"] += 1
        total, complete = _run_tokens(_run_results(run))
        if complete:
            bucket["tokens"].append(float(total))

    report["ensembles"] = {
        name: {
            "runs": bucket["runs"],
            "pass_rate": _pct(bucket["passed"], bucket["runs"]),
            "mean_tokens": _mean(bucket["tokens"]),
            "measured_for_cost": len(bucket["tokens"]),
            "thin": bucket["runs"] < THIN_SAMPLE,
        }
        for name, bucket in sorted(buckets.items(), key=lambda kv: kv[1]["verifier_count"])
    }

    # -- outcomes by recorded setting ----------------------------------------
    settings: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for run in finished:
        recorded = (run.get("settings") or {}) if isinstance(run.get("settings"), dict) else {}
        for key in ("consensus", "isolation", "agent_execution_mode"):
            value = recorded.get(key)
            if value is None:
                continue
            bucket = settings.setdefault(key, {}).setdefault(
                str(value), {"runs": 0, "passed": 0}
            )
            bucket["runs"] += 1
            if str(run.get("verdict")) == "PASS":
                bucket["passed"] += 1

    report["settings"] = {
        key: {
            value: {
                "runs": bucket["runs"],
                "pass_rate": _pct(bucket["passed"], bucket["runs"]),
                "thin": bucket["runs"] < THIN_SAMPLE,
            }
            for value, bucket in sorted(values.items())
        }
        for key, values in sorted(settings.items())
    }

    return report


def _rate(value: Optional[float], thin: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%{' *' if thin else ''}"


def _tokens(value: Optional[float]) -> str:
    return f"{value:,.0f}" if value is not None else "n/a"


# ---------------------------------------------------------------------------
# The evidence, keyed the way a chooser asks for it (Roadmap 8.3)
# ---------------------------------------------------------------------------


def evidence_for_choices(report: Dict[str, Any]) -> Dict[str, Any]:
    """Fold `pairings` into the three keys a person choosing a team actually has. Pure.

    `--stats` prints one row per *pairing* - agent, model and role together - because that is
    the finest grain the run store records. A dropdown does not offer pairings: it offers an
    agent, or a model, or fills a role. So this collapses the same rows three ways and keys
    each map by exactly what the chooser has in hand.

    Nothing is computed here that `compute_stats` did not already observe: this is a
    projection over a projection, which is why it takes a report rather than a run store and
    why it cannot disagree with the table `--stats` prints.

    Returns:
        ``{"by_agent": {...}, "by_model": {"<agent>/<model>": {...}}, "by_role": {...},
           "runs_total": int}`` where each row is
        ``{"runs", "verdicts", "passes", "pass_rate", "tokens_per_run", "thin"}``.
        ``pass_rate`` is a percentage, or ``None`` when nothing was ever verified -
        never zero, because "never judged" and "always failed" are different facts.
    """

    def blank() -> Dict[str, Any]:
        return {"runs": 0, "verdicts": 0, "passes": 0, "token_runs": 0, "token_total": 0.0}

    buckets: Dict[str, Dict[str, Dict[str, Any]]] = {
        "by_agent": {},
        "by_model": {},
        "by_role": {},
    }

    for row in (report or {}).get("pairings") or []:
        agent = str(row.get("agent") or "?")
        model = str(row.get("model") or "-")
        role = str(row.get("role") or "?")
        executions = int(row.get("executions") or 0)
        verdicts = int(row.get("verdicts") or 0)
        rate = row.get("pass_rate")
        passes = int(round(verdicts * float(rate) / 100.0)) if rate is not None else 0
        mean_tokens = row.get("mean_tokens")

        for key_map, key in (
            ("by_agent", agent),
            ("by_model", "%s/%s" % (agent, model)),
            ("by_role", role),
        ):
            bucket = buckets[key_map].setdefault(key, blank())
            bucket["runs"] += executions
            bucket["verdicts"] += verdicts
            bucket["passes"] += passes
            # A mean over executions, weighted back up by how many there were, so that two
            # pairings folded together do not give a rare one the same weight as a common
            # one. Executions that reported no usage stay excluded, as they are upstream.
            if isinstance(mean_tokens, (int, float)):
                bucket["token_runs"] += executions
                bucket["token_total"] += float(mean_tokens) * executions

    evidence: Dict[str, Any] = {"runs_total": int((report or {}).get("runs_total") or 0)}
    for key_map, rows in buckets.items():
        evidence[key_map] = {
            key: {
                "runs": bucket["runs"],
                "verdicts": bucket["verdicts"],
                "passes": bucket["passes"],
                "pass_rate": _pct(bucket["passes"], bucket["verdicts"]),
                "tokens_per_run": (
                    round(bucket["token_total"] / bucket["token_runs"], 1)
                    if bucket["token_runs"]
                    else None
                ),
                "thin": bucket["runs"] < THIN_SAMPLE,
            }
            for key, bucket in rows.items()
        }
    return evidence


def format_stats(report: Dict[str, Any]) -> str:
    """Render the aggregate report as a plain-text table set."""
    lines: List[str] = []
    total = report.get("runs_total", 0)
    if not total:
        return "No recorded runs to analyze. Run a task first, or check run_store.directory."

    lines.append(f"RUNS: {total}" + (f"  ({report['runs_unreadable']} unreadable)" if report.get("runs_unreadable") else ""))
    statuses = report.get("by_status") or {}
    if statuses:
        lines.append("  " + "  ".join(f"{name}={count}" for name, count in sorted(statuses.items())))

    outcomes = report.get("outcomes") or {}
    if outcomes.get("finished"):
        lines.append("")
        lines.append("OUTCOMES")
        lines.append(
            f"  Finished runs:     {outcomes['finished']}\n"
            f"  Passed:            {outcomes['passed']} ({_rate(outcomes.get('pass_rate'))})\n"
            f"  Blocked (needs a person): {outcomes.get('blocked', 0)}\n"
            f"  Budget exhausted:  {outcomes.get('budget_exhausted', 0)}\n"
            f"  Errored:           {outcomes.get('errored', 0)}"
        )

    cost = report.get("cost") or {}
    if cost.get("runs_with_complete_usage"):
        lines.append("")
        lines.append("WHAT AN OUTCOME COSTS (tokens per run, reported usage only)")
        lines.append(
            f"  Pass:  {_tokens(cost.get('mean_tokens_pass'))} mean over {cost.get('passes_measured', 0)} run(s)"
        )
        lines.append(
            f"  Fail:  {_tokens(cost.get('mean_tokens_fail'))} mean over {cost.get('failures_measured', 0)} run(s)"
        )
        if cost.get("runs_with_incomplete_usage"):
            lines.append(
                f"  {cost['runs_with_incomplete_usage']} run(s) excluded: at least one agent reported no usage."
            )

    pairings = report.get("pairings") or []
    if pairings:
        lines.append("")
        lines.append("AGENT / MODEL / ROLE")
        lines.append(
            f"  {'AGENT':<12} {'MODEL':<26} {'ROLE':<12} {'RUNS':>5} {'ERR':>7} {'PASS':>8} {'TOKENS':>10} {'SECS':>7}"
        )
        for row in pairings:
            secs = f"{row['mean_seconds']:.1f}" if row["mean_seconds"] is not None else "n/a"
            lines.append(
                f"  {row['agent']:<12} {row['model'][:26]:<26} {row['role']:<12} "
                f"{row['executions']:>5} {_rate(row['error_rate']):>7} "
                f"{_rate(row['pass_rate'], row['thin']):>8} "
                f"{_tokens(row['mean_tokens']):>10} {secs:>7}"
            )

    repair = report.get("repair") or {}
    by_attempt = repair.get("by_attempt") or []
    if by_attempt:
        lines.append("")
        lines.append("DOES REPAIR PAY OFF?")
        lines.append(f"  Runs that needed at least one repair: {repair.get('runs_with_at_least_one_repair', 0)}")
        for row in by_attempt:
            lines.append(
                f"  Attempt #{row['attempt']}: {row['passed']}/{row['attempted']} "
                f"verified PASS ({_rate(row['pass_rate'], row['thin'])})"
            )

    gate = report.get("gate") or {}
    by_verifier = gate.get("by_verifier") or []
    if by_verifier:
        lines.append("")
        lines.append("VERIFIER AGAINST THE OBJECTIVE CHECK")
        lines.append(f"  Runs with an acceptance gate: {gate.get('runs_with_a_gate', 0)}")
        lines.append(f"  {'VERIFIER':<26} {'JUDGED':>7} {'PASS OVER RED':>14} {'FAIL OVER GREEN':>16}")
        for row in by_verifier:
            lines.append(
                f"  {row['verifier'][:26]:<26} {row['judged']:>7} "
                f"{_rate(row['pass_over_red_rate'], row['thin']):>14} "
                f"{row['fail_over_green']:>16}"
            )

    ensembles = report.get("ensembles") or {}
    if len(ensembles) > 1:
        lines.append("")
        lines.append("ENSEMBLES: OUTCOME AGAINST COST")
        lines.append(f"  {'CONFIGURATION':<20} {'RUNS':>5} {'PASS':>8} {'MEAN TOKENS':>13}")
        for name, row in ensembles.items():
            lines.append(
                f"  {name:<20} {row['runs']:>5} {_rate(row['pass_rate'], row['thin']):>8} "
                f"{_tokens(row['mean_tokens']):>13}"
            )
    elif ensembles:
        only = next(iter(ensembles))
        lines.append("")
        lines.append(f"ENSEMBLES: every recorded run used a {only}; nothing to compare yet.")

    settings = report.get("settings") or {}
    if settings:
        lines.append("")
        lines.append("OUTCOME BY SETTING")
        for key, values in settings.items():
            for value, row in values.items():
                label = f"{key}={value}"
                lines.append(
                    f"  {label:<38} {row['runs']:>4} run(s)  pass {_rate(row['pass_rate'], row['thin'])}"
                )

    if any(
        row.get("thin")
        for row in list(pairings) + list(by_attempt) + list(by_verifier)
        + list((ensembles or {}).values())
    ):
        lines.append("")
        lines.append(f"  * fewer than {THIN_SAMPLE} observations - too thin to conclude anything.")

    return "\n".join(lines)
