"""The cockpit projection: what each member of the team is doing right now (Roadmap Phase 9).

The board (`board.py`) answers *what is the state of everything I have asked for*, organised
by work item. The office (`office.html`) answers *who is working right now*, by watching one
run. The cockpit answers a third question - **what is my team, and what is each member of it
doing** - and it is the first view that can be acted on.

This module is the pure half of that, and it is pure for the same reason `board.py` is: a
projection that can be tested without a server, a browser, or a run. It takes a team (which
`teams.py` already reads out of `orchestrator.yaml`) and a run's events (which `store.py`
already appends), and returns one column per configured role with one card per agent.

What it does not do
-------------------
* It invents no status vocabulary. A card is `idle`, `working`, or carries the verdict its
  agent actually produced - all of it derived from `agent_started` and `agent_result`, the
  two events Phase 4 added so a reader could see work in progress rather than guess at it.
* It stores nothing. Every value here is recomputed from events on each read, so there is no
  second place an agent's state can be wrong.
* It decides no order. Columns are `teams.phases_of()`'s order, which is `graph.py`'s
  execution order, which is why "left to right" is a true statement about the page rather
  than a metaphor.
"""

from typing import Any, Dict, List, Optional

from orchestrator.config import ladder_rungs
from orchestrator.teams import phases_of
from orchestrator.types import result_token_rows, result_tokens_spent

#: An agent that has not been launched in the run being watched.
AGENT_IDLE = "idle"
#: Launched, and no result recorded yet.
AGENT_WORKING = "working"
#: Finished, and its result carried a passing verdict (or no verdict at all - a researcher
#: does not return one, and finishing is all it claims).
AGENT_DONE = "done"
#: Finished, and its result carried a failing verdict or an error.
AGENT_FAILED = "failed"

AGENT_STATES = (AGENT_IDLE, AGENT_WORKING, AGENT_DONE, AGENT_FAILED)

#: Verdicts `graph.py` records that mean the work did not pass.
FAILING_VERDICTS = ("FAIL", "ERROR", "BLOCKED", "TIMEOUT")


def agent_key(agent: Optional[str], role: Optional[str], index: int = 0) -> str:
    """A stable identity for one card. Pure.

    An ensemble is several entries with the same role and often the same provider, so neither
    alone identifies a card. The position in the team is what distinguishes them, and the
    position is what `orchestrator.yaml` already fixes.
    """
    return "%s:%s:%d" % (str(agent or ""), str(role or ""), int(index))


def _model_label(model: Any) -> str:
    """How a model, or a ladder of them, reads on a card. Pure."""
    if isinstance(model, list):
        return " -> ".join(str(m) for m in model if str(m).strip())
    return str(model or "")


def _agent_label(agent: Any) -> str:
    """How an agent, or a ladder of them, reads on a card. Pure."""
    if isinstance(agent, list):
        return " -> ".join(str(a) for a in agent if str(a).strip())
    return str(agent or "")


def _latest_state(
    states: Dict[str, Dict[str, Any]],
    rungs: List[Any],
    role: Optional[str],
) -> Dict[str, Any]:
    """The live state for a card, across every agent its ladder could run as. Pure.

    Events are keyed by the agent that actually ran, so a card whose ladder escalated has
    state under two different keys. The one that happened *last* is the card's state; ties
    and absences fall back to an empty dict, which reads as idle.
    """
    best: Dict[str, Any] = {}
    best_sequence = -1
    for agent, _model in rungs:
        candidate = states.get("%s/%s" % (agent, role))
        if not candidate:
            continue
        sequence = candidate.get("finished_sequence")
        if sequence is None:
            sequence = candidate.get("started_sequence")
        try:
            sequence = int(sequence)
        except (TypeError, ValueError):
            sequence = 0
        if sequence >= best_sequence:
            best, best_sequence = candidate, sequence
    return best


def agent_states(events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Derive what each (agent, role) pair is doing, from one run's events. Pure.

    Keyed by ``agent/role`` rather than by card, because that is all an event carries: the
    pipeline records *which agent in which role* started, not which entry of an ensemble. A
    role with several identical agents therefore shows the same state on each of its cards,
    which is honest - the event log genuinely does not distinguish them.
    """
    states: Dict[str, Dict[str, Any]] = {}

    for event in events or []:
        name = str(event.get("event") or "")

        if name == "agent_started":
            key = "%s/%s" % (event.get("agent"), event.get("role"))
            states[key] = {
                "state": AGENT_WORKING,
                "agent": event.get("agent"),
                "role": event.get("role"),
                "model": event.get("model"),
                "repair_attempt": event.get("repair_attempt"),
                "verdict": None,
                "tokens": (states.get(key) or {}).get("tokens", 0),
                "started_sequence": event.get("sequence"),
            }
            continue

        if name == "agent_result":
            result = event.get("result") or {}
            key = "%s/%s" % (result.get("agent"), result.get("role"))
            previous = states.get(key) or {}
            verdict = str(result.get("verdict") or "").upper()
            # An AgentResult's only failure signal for most roles is `status == "error"`
            # (types.py) - only a verifier ever carries a verdict, and there is no "error"
            # field on the dict at all. Missing either check here is exactly how a
            # researcher's hard failure used to be drawn as a green, finished card.
            failed = str(result.get("status") or "") == "error" or verdict in FAILING_VERDICTS
            # Everything this execution paid for, its failed retry/escalation attempts included,
            # read the one way every other total reads it (types.result_tokens_spent).
            tokens = previous.get("tokens", 0) + result_tokens_spent(result)[0]
            states[key] = {
                "state": AGENT_FAILED if failed else AGENT_DONE,
                "agent": result.get("agent"),
                "role": result.get("role"),
                "model": result.get("model") or previous.get("model"),
                "repair_attempt": previous.get("repair_attempt"),
                "verdict": result.get("verdict"),
                "error": result.get("output") if failed else None,
                "duration_seconds": result.get("duration_seconds"),
                "tokens": tokens,
                "started_sequence": previous.get("started_sequence"),
                "finished_sequence": event.get("sequence"),
            }

    return states


def columns(
    team: Dict[str, Any],
    events: Optional[List[Dict[str, Any]]] = None,
    terminals: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """One column per configured role, one card per agent in it. Pure.

    Args:
        team: What `teams.team_from_config` returns.
        events: The run being watched, or None for a team at rest.
        terminals: Optional map of ``agent/role`` to a live terminal id (Phase 10), so a card
            can offer to open the stream the agent is actually writing to.

    Returns:
        Columns in pipeline order, each with its agents and a derived column state.
    """
    phases = team.get("phases") or phases_of(team.get("agents") or [])
    states = agent_states(events or [])
    terminals = terminals or {}

    out: List[Dict[str, Any]] = []
    for phase in phases:
        cards: List[Dict[str, Any]] = []
        for index, entry in enumerate(phase.get("agents") or []):
            rungs = ladder_rungs(entry)
            first_agent = rungs[0][0]
            # An agent ladder means one card can be worked by more than one agent, so the
            # card follows whichever rung the run actually reached - the most recent event
            # wins. Reading rung 0 alone would draw an escalated card as still idle.
            live = _latest_state(states, rungs, entry.get("role"))
            # The terminal registry keys its streams by the agent that actually launched
            # them (terminals.py's live_by_agent), so the lookup below has to follow the
            # same escalated agent `live` does - rung 0 alone would go stale the moment a
            # run escalates past it.
            terminal_agent = live.get("agent") or first_agent
            terminal_key = "%s/%s" % (terminal_agent, entry.get("role"))
            # A card's spend is every rung's, not only the rung it ended on: an escalation's
            # results are recorded under whichever agent produced them.
            rung_agents = list(dict.fromkeys(str(agent) for agent, _model in rungs))
            card_tokens = sum(
                int((states.get("%s/%s" % (agent, entry.get("role"))) or {}).get("tokens") or 0)
                for agent in rung_agents
            )
            cards.append(
                {
                    "key": agent_key(first_agent, entry.get("role"), index),
                    "agent": live.get("agent") or first_agent,
                    "configured_agent": entry.get("agent"),
                    "agent_label": _agent_label(entry.get("agent")),
                    "role": entry.get("role"),
                    "model": entry.get("model"),
                    "model_label": _model_label(entry.get("model")),
                    "ladder": len(rungs) > 1,
                    "ladder_depth": len(rungs),
                    "state": live.get("state") or AGENT_IDLE,
                    "verdict": live.get("verdict"),
                    "error": live.get("error"),
                    "tokens": card_tokens,
                    "duration_seconds": live.get("duration_seconds"),
                    "repair_attempt": live.get("repair_attempt"),
                    "terminal_id": terminals.get(terminal_key),
                }
            )

        out.append(
            {
                "index": phase.get("index"),
                "role": phase.get("role"),
                "parallel": bool(phase.get("parallel")),
                "agents": cards,
                "state": column_state(cards),
            }
        )
    return out


def run_tokens(events: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """What the watched run has spent, read from its results the way `--stats` and the CLI's
    summary table read them. Pure.

    The page used to add up its cards, and a card is not a unit of spend: identical members of
    an ensemble share one event key (see `agent_states`), so their cards show the same figure
    and summing the cards counted it once per card.

    Returns ``{known, complete, failed_attempt_tokens, failed_attempts}``: `known` is every
    reported token, failed attempts included; `complete` is False when any execution or attempt
    reported nothing, in which case `known` is a floor rather than the total (invariant 5).
    """
    known = 0
    complete = True
    failed_tokens = 0
    failed_attempts = 0
    for event in events or []:
        if event.get("event") != "agent_result":
            continue
        for attempt, total in result_token_rows(event.get("result") or {}):
            if attempt is not None:
                failed_attempts += 1
            if total is None:
                complete = False
                continue
            known += total
            if attempt is not None:
                failed_tokens += total
    return {
        "known": known,
        "complete": complete,
        "failed_attempt_tokens": failed_tokens,
        "failed_attempts": failed_attempts,
    }


def column_state(cards: List[Dict[str, Any]]) -> str:
    """What a whole column is doing, from its cards. Pure.

    A column is working while any of its agents is. Once every member has finished, this
    matches `make_sync_node`'s own fatal condition exactly - a phase is fatal only when *all*
    of its members failed (invariant 8: "one dead member of an ensemble is a warning, not the
    end of the run"). A verified live run makes this concrete: an ensemble of two researchers
    where one errored and one succeeded continued straight through planner, implementer and
    verifier to a PASS. An earlier version read any failed member as the column's failure,
    which would have shown a solid red "researcher" column the entire time that run was
    genuinely succeeding. The failed member's own card still shows failed - the warning is
    not hidden, only not mistaken for the phase's fate.
    """
    values = [str(card.get("state") or AGENT_IDLE) for card in cards]
    if not values:
        return AGENT_IDLE
    if AGENT_WORKING in values:
        return AGENT_WORKING
    if AGENT_IDLE in values:
        # Not every member has started or reported yet - whether the phase survives cannot
        # be answered until it can be judged the way `make_sync_node` judges it: on every
        # member's outcome at once.
        return AGENT_WORKING if (AGENT_DONE in values or AGENT_FAILED in values) else AGENT_IDLE
    return AGENT_DONE if AGENT_DONE in values else AGENT_FAILED


def needs_you(board: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cards a person has to answer, in the words the board already gives them. Pure.

    Phase 5 built the gates and Phase 4 built the column; this only lifts that column to
    where a person is already looking, which is Roadmap §8.5.
    """
    from orchestrator.board import COLUMN_NEEDS_YOU

    board_columns = (board or {}).get("columns") or {}
    return list(board_columns.get(COLUMN_NEEDS_YOU) or [])


def ready_to_merge(board: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The cards that could be delivered right now. Pure."""
    from orchestrator.board import COLUMN_READY

    board_columns = (board or {}).get("columns") or {}
    return list(board_columns.get(COLUMN_READY) or [])
