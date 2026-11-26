"""Exp-10-01. Deterministic in-memory simulation, not a model benchmark."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path
import argparse
import hashlib
import json
import platform
import sys
import time

import yaml

from revision import Budget, Node, Observation, Plan, checkpoint, revise, validate

HERE = Path(__file__).resolve().parent
STEPS = ['receive_goal', 'read_policy', 'check_conditions', 'draft_request',
         'register_request', 'check_resource', 'assemble_evidence', 'submit_approval',
         'read_status', 'verify_goal', 'archive']


def write_jsonl(path, rows):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True)+'\n' for r in rows), encoding='utf-8')


def fixtures():
    kinds = ['goal_change']*3 + ['tool_failure']*3 + ['environment_change']*2 + ['stall', 'unknown_outcome']
    tasks = []
    for i, kind in enumerate(kinds, 1):
        task_id = f'ch10-local-{i:02d}'
        nodes = [Node(f'n{s}', (f'n{s-1}',) if s > 1 else (), s,
                      effect_key=f'{task_id}:register' if s == 5 else None) for s in range(1, 12)]
        tasks.append(dict(task_id=task_id, kind=kind, inject_before_step=6, initial_ticks=40,
                          recoverable=kind not in {'stall', 'unknown_outcome'},
                          steps=STEPS, nodes=[asdict(n) for n in nodes]))
    return tasks


class MockWorld:
    """Owns effects and final truth independently of planner completion flags."""
    def __init__(self):
        self.goal_version = 0
        self.capability = 'primary'
        self.resource = 'original'
        self.results = {}
        self.effects = Counter()

    def inject(self, kind):
        if kind == 'goal_change':
            self.goal_version = 1
        if kind == 'tool_failure':
            self.capability = 'backup'
        if kind == 'environment_change':
            self.resource = 'substitute'

    def execute(self, node, kind, injected, guard=True):
        if injected and node.step == 6:
            if kind in {'stall', 'unknown_outcome'}:
                return False
            if (node.goal_version, node.capability, node.resource) != (self.goal_version, self.capability, self.resource):
                return False
        if node.effect_key:
            if guard and self.effects[node.effect_key]:
                return True  # executor reconciliation; does not apply effect twice
            self.effects[node.effect_key] += 1
        self.results[node.step] = (node.goal_version, node.capability, node.resource)
        return True

    def accepted(self):
        expected = (self.goal_version, self.capability, self.resource)
        return (set(self.results) == set(range(1, 12))
                and all(self.results[s] == expected for s in range(6, 12))
                and all(v == 1 for v in self.effects.values()) and bool(self.effects))


def run(task, strategy, policy):
    nodes = tuple(Node(**{**n, 'deps': tuple(n['deps'])}) for n in task['nodes'])
    plan = Plan(1, nodes)
    initial_ticks = min(task['initial_ticks'], policy['initial_ticks'])
    if policy['action_ticks'] <= 0 or policy['checkpoint_ticks'] <= 0:
        raise ValueError('Action/checkpoint costs must be positive')
    budget = Budget(initial_ticks, max_revisions=policy['max_revisions'],
                    max_no_progress=policy['max_no_progress'])
    completed, consumed = set(), set()
    world = MockWorld()
    traces, revisions = [], []
    injected, injection_ticks = False, None
    status, waits = 'RUNNING', 0

    def trace(event, **extra):
        traces.append(dict(task_id=task['task_id'], strategy=strategy, event=event,
                           tick=initial_ticks-budget.remaining,
                           plan_version=plan.version, remaining_budget=budget.remaining,
                           no_progress=budget.no_progress, **extra))

    while status == 'RUNNING':
        if budget.remaining < policy['action_ticks'] + policy['checkpoint_ticks']:
            status = 'STOP_BUDGET'
            break
        order = validate(plan)
        ready = next((n for k in order for n in plan.nodes
                      if n.id == k and n.id not in completed and set(n.deps) <= completed), None)
        if ready is None:
            status = 'ACCEPT' if world.accepted() else 'REJECTED_FINAL_EVIDENCE'
            break
        if ready.step >= task['inject_before_step'] and not injected:
            injected = True
            world.inject(task['kind'])
            injection_ticks = initial_ticks-budget.remaining
            observation = Observation(f"{task['task_id']}:obs-1", task['kind'],
                                      world.goal_version, world.capability, world.resource,
                                      outcome_unknown=task['kind']=='unknown_outcome')
            # This read-only checkpoint observes the change; it is not action completion.
            decision = checkpoint(budget, progressed=False, outcome_unknown=observation.outcome_unknown,
                                  ticks=policy['checkpoint_ticks'])
            trace('injection', observation=asdict(observation), decision=decision)
            if decision.startswith('STOP') or decision == 'ESCALATE':
                status = decision
                break
            if strategy in {'local', 'global'} and task['recoverable']:
                try:
                    plan, log = revise(plan, completed, observation, {ready.id}, strategy, budget, consumed,
                                       reserve_ticks=policy['action_ticks']+policy['checkpoint_ticks'])
                    revisions.append(dict(task_id=task['task_id'], **log))
                    trace('revision', observation_id=observation.id, retired=log['retired'], added=log['added'])
                except ValueError as e:
                    status = 'ESCALATE_REVISION_REJECTED'
                    trace('revision_rejected', reason=str(e))
                    break
                continue
            if strategy == 'unsafe_full' and task['recoverable']:
                # Intentional negative control: drops completion ledger and replays whole plan.
                if len(plan.nodes) >= budget.remaining:
                    status = 'STOP_BUDGET'
                    break
                budget.charge(len(plan.nodes))
                budget.revisions += 1
                completed.clear()
                plan = Plan(2, tuple(Node(n.id+'@unsafe', tuple(d+'@unsafe' for d in n.deps),
                                         n.step, world.goal_version, world.capability,
                                         world.resource, n.effect_key) for n in plan.nodes), 1)
                trace('unsafe_restart', warning='sandbox-only negative control')
                continue
        budget.charge(policy['action_ticks'])
        success = world.execute(ready, task['kind'], injected, guard=strategy!='unsafe_full')
        if success:
            completed.add(ready.id)
        else:
            waits += 1
        decision = checkpoint(budget, progressed=success, waiting=not success,
                              goal_verified=world.accepted(), ticks=policy['checkpoint_ticks'])
        trace('action', node_id=ready.id, step=ready.step, action_name=STEPS[ready.step-1],
              success=success, decision=decision, effects=dict(world.effects))
        if decision.startswith('STOP') or decision in {'ESCALATE', 'ACCEPT'}:
            status = decision
    trace('terminal', status=status, business_verified=world.accepted())
    spent = initial_ticks-budget.remaining
    return dict(task_id=task['task_id'], strategy=strategy, kind=task['kind'],
                recoverable=task['recoverable'], status=status, success=status=='ACCEPT' and world.accepted(),
                duplicate_effects=sum(max(0,v-1) for v in world.effects.values()),
                revisions=budget.revisions, waits=waits, elapsed_ticks=spent,
                recovery_ticks=spent-injection_ticks if injection_ticks is not None else None,
                remaining_budget=budget.remaining), traces, revisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=HERE/'results')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    policy = yaml.safe_load((HERE/'checkpoint_policy.yaml').read_text(encoding='utf-8'))
    tasks = fixtures()
    # Input is checked in and read back to avoid evaluating a different in-memory fixture.
    input_path = HERE/'task_chains.jsonl'
    if not input_path.exists():
        write_jsonl(input_path, tasks)
    tasks = [json.loads(line) for line in input_path.read_text(encoding='utf-8').splitlines()]
    started = time.perf_counter()
    rows, traces, revisions = [], [], []
    for strategy in ['fixed', 'global', 'local', 'unsafe_full']:
        for task in tasks:
            row, events, logs = run(task, strategy, policy)
            rows.append(row)
            traces.extend(events)
            revisions.extend(logs)
    summary = {}
    for strategy in ['fixed', 'global', 'local', 'unsafe_full']:
        group = [r for r in rows if r['strategy']==strategy]
        summary[strategy] = dict(denominator=len(group), successes=sum(r['success'] for r in group),
                                recoverable_denominator=sum(r['recoverable'] for r in group),
                                recoverable_successes=sum(r['success'] and r['recoverable'] for r in group),
                                duplicate_effects=sum(r['duplicate_effects'] for r in group),
                                revisions=sum(r['revisions'] for r in group),
                                mean_recovery_ticks=sum(r['recovery_ticks'] for r in group)/len(group),
                                mean_remaining_budget=sum(r['remaining_budget'] for r in group)/len(group),
                                terminal_counts=dict(Counter(r['status'] for r in group)))
    write_jsonl(args.out/'runs.jsonl', rows)
    write_jsonl(args.out/'traces.jsonl', traces)
    # Dedicated asset path is stable, even if aggregate output goes to a reproduction dir.
    write_jsonl(args.out/'plan_revision_log.jsonl', revisions)
    if args.out.resolve() == (HERE/'results').resolve():
        write_jsonl(HERE/'plan_revision_log.jsonl', revisions)
    (args.out/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    keys = ['strategy','denominator','successes','recoverable_denominator','recoverable_successes',
            'duplicate_effects','revisions','mean_recovery_ticks','mean_remaining_budget']
    (args.out/'Tab-10-02.tsv').write_text('\t'.join(keys)+'\n'+''.join('\t'.join(str(s[k]) if k!='strategy' else key for k in keys)+'\n' for key,s in summary.items()), encoding='utf-8')
    paths = [HERE/'revision.py', HERE/'run_experiment.py', HERE/'checkpoint_policy.yaml', input_path]
    env = dict(python=sys.version, platform=platform.platform(), yaml_version=yaml.__version__,
               wall_seconds=time.perf_counter()-started, schema_status='ch10-proposal-v0',
               evidence_level='deterministic_mock', sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    (args.out/'environment.json').write_text(json.dumps(env, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
