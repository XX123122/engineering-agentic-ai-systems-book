"""Case-10-05: one branched DAG and two observations, deterministic mock only."""
from dataclasses import asdict
from pathlib import Path
import json

from revision import Budget, Node, Observation, Plan, checkpoint, revise, validate


def initial_plan():
    return Plan(1, (
        Node('r', step=1), Node('p', ('r',), step=2),
        Node('w', ('p',), step=3, effect_key='ch10-branch:ticket'),
        Node('a', ('w',), step=4), Node('b', ('a',), step=5),
        Node('i', ('w',), step=6), Node('j', ('i',), step=7),
        Node('m', ('b', 'j'), step=8), Node('q', ('m',), step=9),
        Node('v', ('q',), step=10), Node('z', ('v',), step=11),
    ))


def demonstrate():
    plan = initial_plan()
    # Supplied case precondition: r/p/w already have confirmed evidence.
    # This is not a remote ticket operation or verification of a real ticket.
    completed = {'r', 'p', 'w'}
    consumed = set()
    budget = Budget()
    original = {n.id:n for n in plan.nodes}
    first = Observation('branch:o1', 'environment_change', resource='substitute')
    assert checkpoint(budget, progressed=False) == 'CONTINUE'
    local, log1 = revise(plan, completed, first, {'a'}, 'local', budget, consumed)
    global_budget = Budget()
    assert checkpoint(global_budget, progressed=False) == 'CONTINUE'
    _, global_log = revise(plan, completed, first, {'a'}, 'global', global_budget, set())
    events = []
    # Four fixed read-only mock results, all dependencies checked at execution.
    for node_id in ['a@v2', 'b@v2', 'i', 'j']:
        node = next(n for n in local.nodes if n.id == node_id)
        assert node.effect_key is None and set(node.deps) <= completed
        budget.charge(1)
        completed.add(node_id)
        decision = checkpoint(budget, progressed=True)
        assert decision == 'CONTINUE'
        events.append(dict(event='readonly_mock_result', node_id=node_id,
                           remaining_budget=budget.remaining, decision=decision))
    second = Observation('branch:o2', 'goal_change', goal_version=1, resource='substitute')
    assert checkpoint(budget, progressed=False) == 'CONTINUE'
    final, log2 = revise(local, completed, second, {'m@v2'}, 'local', budget, consumed)
    before = (budget.remaining, budget.revisions, set(consumed))
    try:
        revise(final, completed, second, {'m@v2@v3'}, 'local', budget, consumed)
    except ValueError as exc:
        reused_rejected = str(exc)
    else:
        raise AssertionError('Reused observation was accepted')
    assert (budget.remaining, budget.revisions, consumed) == before
    final_nodes = {n.id:n for n in final.nodes}
    assert all(final_nodes[k] is original[k] for k in ['r','p','w','i','j'])
    assert final_nodes['m@v2@v3'].deps == ('b@v2','j')
    assert set(validate(final)) == final_nodes.keys()
    summary = dict(evidence_level='deterministic_mock',case_id='Case-10-05',
                   first_local_replacements=len(log1['added']),
                   first_global_replacements=len(global_log['added']),
                   first_local_remaining=log1['remaining_budget'],
                   first_global_remaining=global_log['remaining_budget'],
                   after_four_reads=events[-1]['remaining_budget'],
                   second_replacements=len(log2['added']),remaining_budget=budget.remaining,
                   revisions=budget.revisions,completed=sorted(completed),
                   no_progress=budget.no_progress,
                   reused_observation_rejected=reused_rejected,
                   business_task_accepted=False,
                   note='Graph mechanism only; final future nodes are not executed.')
    return summary, [log1,log2], events


if __name__ == '__main__':
    summary, logs, events = demonstrate()
    out = Path(__file__).resolve().parent/'branch_results'
    out.mkdir(exist_ok=True)
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'revision_log.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in logs),encoding='utf-8')
    (out/'read_events.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in events),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
