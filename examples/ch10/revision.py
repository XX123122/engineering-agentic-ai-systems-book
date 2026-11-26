"""Ch10 isolated teaching proposal. No network or shared schema dependency."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections import deque


@dataclass(frozen=True)
class Node:
    id: str
    deps: tuple[str, ...] = ()
    step: int = 0
    goal_version: int = 0
    capability: str = 'primary'
    resource: str = 'original'
    effect_key: str | None = None


@dataclass(frozen=True)
class Plan:
    version: int
    nodes: tuple[Node, ...]
    parent: int | None = None
    retired: tuple[str, ...] = ()


@dataclass(frozen=True)
class Observation:
    id: str
    kind: str
    goal_version: int = 0
    capability: str = 'primary'
    resource: str = 'original'
    fresh: bool = True
    outcome_unknown: bool = False


@dataclass
class Budget:
    remaining: int = 40
    revisions: int = 0
    no_progress: int = 0
    max_revisions: int = 3
    max_no_progress: int = 3

    def charge(self, ticks: int) -> None:
        if ticks <= 0:
            raise ValueError('Costs must be positive')
        if ticks > self.remaining:
            raise ValueError('Budget exhausted')
        self.remaining -= ticks


def validate(plan: Plan) -> list[str]:
    """Return deterministic topological order, rejecting cycles/dangling edges."""
    by_id = {n.id: n for n in plan.nodes}
    if len(by_id) != len(plan.nodes) or not by_id:
        raise ValueError('Duplicate node IDs or empty plan')
    if set(by_id) & set(plan.retired):
        raise ValueError('Retired node is still active')
    children: dict[str, list[str]] = {k: [] for k in by_id}
    degrees = {}
    for node in plan.nodes:
        if len(node.deps) != len(set(node.deps)):
            raise ValueError('Duplicate dependency')
        if any(d not in by_id for d in node.deps):
            raise ValueError('Dangling dependency')
        degrees[node.id] = len(node.deps)
        for d in node.deps:
            children[d].append(node.id)
    ready = deque(k for k in by_id if degrees[k] == 0)
    order = []
    while ready:
        k = ready.popleft()
        order.append(k)
        for child in children[k]:
            degrees[child] -= 1
            if degrees[child] == 0:
                ready.append(child)
    if len(order) != len(by_id):
        raise ValueError('Cycle detected')
    return order


def affected_nodes(plan: Plan, roots: set[str], completed: set[str]) -> set[str]:
    """Conservative closure: every unfinished descendant must be replaced."""
    by_id = {n.id: n for n in plan.nodes}
    if not roots or not roots <= by_id.keys():
        raise ValueError('Unknown or empty invalidation roots')
    if roots & completed:
        raise ValueError('Executed history cannot be retired')
    children = {k: [] for k in by_id}
    for n in plan.nodes:
        for d in n.deps:
            children[d].append(n.id)
    seen, queue = set(roots), deque(roots)
    while queue:
        for c in children[queue.popleft()]:
            if c in completed:
                raise ValueError('Invalidation crosses executed descendant; escalate')
            if c not in seen:
                seen.add(c)
                queue.append(c)
    return seen


def revise(plan: Plan, completed: set[str], observation: Observation,
           roots: set[str], mode: str, budget: Budget,
           consumed: set[str], protected: set[str] | None = None,
           reserve_ticks: int = 2) -> tuple[Plan, dict]:
    """Validate candidate before changing budget/consumed. Single-threaded only.

    protected includes applied-but-unaccepted actions; never silently replay them.
    Planning cost is the number of replacement nodes (synthetic ticks).
    """
    validate(plan)
    active_ids = {n.id for n in plan.nodes}
    if not completed <= active_ids:
        raise ValueError('Completion ID absent from active plan')
    if any(n.id in completed and not set(n.deps) <= completed for n in plan.nodes):
        raise ValueError('Completion set is not dependency-closed')
    if observation.id in consumed or not observation.fresh:
        raise ValueError('Reused or stale observation')
    if observation.outcome_unknown:
        raise ValueError('Unknown side effect; escalate for reconciliation')
    if budget.remaining <= 0 or budget.no_progress >= budget.max_no_progress:
        raise ValueError('Terminal budget or no-progress boundary')
    if budget.revisions >= budget.max_revisions:
        raise ValueError('Revision limit reached')
    if mode not in {'local', 'global'}:
        raise ValueError('Unknown repair strategy')
    local_scope = affected_nodes(plan, roots, completed)
    scope = local_scope if mode == 'local' else active_ids - completed
    if scope & (protected or set()):
        raise ValueError('Applied side effect is not accepted; reconcile first')
    new_version = plan.version + 1
    rename = {k: f'{k}@v{new_version}' for k in scope}
    nodes = []
    for n in plan.nodes:
        if n.id in scope:
            nodes.append(replace(n, id=rename[n.id], deps=tuple(rename.get(d, d) for d in n.deps),
                                 goal_version=observation.goal_version,
                                 capability=observation.capability, resource=observation.resource))
        else:
            nodes.append(n)
    candidate = Plan(new_version, tuple(nodes), plan.version, plan.retired + tuple(sorted(scope)))
    validate(candidate)
    cost = len(scope)
    if reserve_ticks <= 0 or cost + reserve_ticks > budget.remaining:
        raise ValueError('No execution budget after planning')
    budget.charge(cost)
    budget.revisions += 1
    consumed.add(observation.id)
    old_edges = {(d, n.id) for n in plan.nodes for d in n.deps}
    new_edges = {(d, n.id) for n in candidate.nodes for d in n.deps}
    log = dict(schema_status='ch10-proposal-v0', old_version=plan.version,
               new_version=candidate.version, observation=asdict(observation),
               strategy=mode, kept_completed=sorted(completed), retired=sorted(scope),
               added=sorted(rename.values()), removed_edges=sorted(old_edges-new_edges),
               added_edges=sorted(new_edges-old_edges), planning_ticks=cost,
               remaining_budget=budget.remaining)
    return candidate, log


def checkpoint(budget: Budget, *, progressed: bool, waiting: bool = False,
               fresh: bool = True, outcome_unknown: bool = False,
               goal_verified: bool = False, ticks: int = 1) -> str:
    """Unknown outcome outranks completion. Every checkpoint has positive cost."""
    if ticks <= 0:
        raise ValueError('Checkpoint cost must be positive')
    if budget.remaining <= 0:
        return 'ESCALATE' if outcome_unknown else 'STOP_BUDGET'
    if budget.remaining < ticks:
        return 'ESCALATE' if outcome_unknown else 'STOP_BUDGET'
    budget.charge(ticks)
    if outcome_unknown:
        return 'ESCALATE'
    if budget.remaining <= 0:
        return 'STOP_BUDGET'
    if fresh and progressed:
        budget.no_progress = 0
    else:
        budget.no_progress += 1
    if goal_verified and fresh:
        return 'ACCEPT'
    if budget.remaining <= 0:
        return 'STOP_BUDGET'
    if budget.no_progress >= budget.max_no_progress:
        return 'STOP_NO_PROGRESS'
    return 'WAIT' if waiting or not fresh else 'CONTINUE'
