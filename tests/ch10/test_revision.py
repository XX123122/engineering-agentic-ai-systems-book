"""Behavior tests for Ch10 teaching safety boundaries and experiment accounting."""
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'examples/ch10'
sys.path.insert(0, str(EXAMPLE))
from revision import Budget, Node, Observation, Plan, checkpoint, revise, validate
from run_experiment import MockWorld, fixtures, run
from branch_revision import demonstrate


@pytest.fixture
def branch():
    return Plan(1, (Node('a', effect_key='business-key'), Node('b', ('a',)),
                    Node('c', ('b',)), Node('independent', ('a',))))


def test_local_preserves_completed_effect_and_unrelated_future(branch):
    budget = Budget()
    new, log = revise(branch, {'a'}, Observation('o1','tool_failure'), {'b'}, 'local', budget, set())
    assert new.nodes[0] is branch.nodes[0]
    assert new.nodes[-1] is branch.nodes[-1]
    assert new.retired == ('b','c')
    assert new.nodes[2].deps == ('b@v2',)
    assert 'a' in validate(new)
    assert log['removed_edges'] and log['added_edges']
    assert log['planning_ticks'] == 2
    assert budget.remaining == 38 and budget.revisions == 1
    assert branch.version == 1 and branch.retired == ()


def test_global_rebuilds_all_future_but_not_history(branch):
    new, log = revise(branch, {'a'}, Observation('o1','goal_change',1), {'b'}, 'global', Budget(), set())
    assert new.nodes[0] is branch.nodes[0]
    assert set(new.retired) == {'b','c','independent'}
    assert log['planning_ticks'] == 3


@pytest.mark.parametrize('plan', [
    Plan(1,(Node('a',('b',)),Node('b',('a',)))),
    Plan(1,(Node('a',('missing',)),)),
    Plan(1,(Node('a'),Node('a'))),
    Plan(1,(Node('a'),),retired=('a',)),
    Plan(1,(Node('a'),Node('b',('a','a')))),
])
def test_invalid_graph_is_rejected(plan):
    with pytest.raises(ValueError):
        validate(plan)


@pytest.mark.parametrize('budget,obs,completed,roots,consumed,protected', [
    (Budget(0), Observation('o','failure'), {'a'}, {'b'}, set(), set()),
    (Budget(2), Observation('o','failure'), {'a'}, {'b'}, set(), set()),
    (Budget(revisions=3), Observation('o','failure'), {'a'}, {'b'}, set(), set()),
    (Budget(no_progress=3), Observation('o','failure'), {'a'}, {'b'}, set(), set()),
    (Budget(), Observation('o','failure',fresh=False), {'a'}, {'b'}, set(), set()),
    (Budget(), Observation('o','failure',outcome_unknown=True), {'a'}, {'b'}, set(), set()),
    (Budget(), Observation('o','failure'), {'a'}, {'b'}, {'o'}, set()),
    (Budget(), Observation('o','failure'), {'a'}, {'a'}, set(), set()),
    (Budget(), Observation('o','failure'), {'a'}, {'missing'}, set(), set()),
    (Budget(), Observation('o','failure'), {'c'}, {'b'}, set(), set()),
    (Budget(), Observation('o','failure'), {'a'}, {'b'}, set(), {'b'}),
])
def test_rejected_revision_leaves_state_unchanged(branch,budget,obs,completed,roots,consumed,protected):
    before = (budget.remaining,budget.revisions,set(consumed))
    with pytest.raises(ValueError):
        revise(branch,completed,obs,roots,'local',budget,consumed,protected)
    assert (budget.remaining,budget.revisions,consumed) == before
    assert branch.version == 1


def test_fresh_progress_resets_stall_but_stale_progress_does_not():
    b = Budget(no_progress=2)
    assert checkpoint(b,progressed=True,fresh=False) == 'STOP_NO_PROGRESS'
    b = Budget(no_progress=2)
    assert checkpoint(b,progressed=True) == 'CONTINUE'
    assert b.no_progress == 0


def test_waits_are_bounded_and_cost_budget():
    b = Budget()
    assert [checkpoint(b,progressed=False,waiting=True) for _ in range(3)] == ['WAIT','WAIT','STOP_NO_PROGRESS']
    assert b.remaining == 37


def test_zero_budget_and_unknown_results_override_success():
    assert checkpoint(Budget(0),progressed=True,goal_verified=True) == 'STOP_BUDGET'
    assert checkpoint(Budget(),progressed=True,goal_verified=True,outcome_unknown=True) == 'ESCALATE'
    assert checkpoint(Budget(1),progressed=True,goal_verified=True) == 'STOP_BUDGET'


def test_checkpoint_charges_configured_cost_and_unknown_observation():
    b = Budget(10)
    assert checkpoint(b,progressed=False,ticks=2) == 'CONTINUE'
    assert b.remaining == 8
    assert checkpoint(b,progressed=False,outcome_unknown=True,ticks=2) == 'ESCALATE'
    assert b.remaining == 6
    with pytest.raises(ValueError):
        checkpoint(b,progressed=False,ticks=0)


def test_branched_case_two_revisions_preserve_history_and_merge_dependencies():
    summary, logs, events = demonstrate()
    assert summary['first_local_replacements'] == 6
    assert summary['first_global_replacements'] == 8
    assert summary['first_local_remaining'] == 33
    assert summary['first_global_remaining'] == 31
    assert [e['remaining_budget'] for e in events] == [31,29,27,25]
    assert summary['second_replacements'] == 4
    assert summary['remaining_budget'] == 20 and summary['revisions'] == 2
    assert summary['no_progress'] == 1
    assert logs[1]['old_version'] == 2 and logs[1]['new_version'] == 3
    assert ('b@v2','m@v2@v3') in logs[1]['added_edges']
    assert ('j','m@v2@v3') in logs[1]['added_edges']
    assert not summary['business_task_accepted']
    assert summary['reused_observation_rejected'] == 'Reused or stale observation'


def test_executor_deduplicates_same_business_effect():
    w = MockWorld()
    n = Node('id1',step=5,effect_key='business-key')
    w.execute(n,'goal_change',False)
    w.execute(replace(n,id='id2'),'goal_change',False)
    assert w.effects['business-key'] == 1
    assert not w.accepted()  # transport success and replay guard are not task completion


def test_mock_verifier_checks_goal_results_and_duplicate_effects():
    w = MockWorld()
    w.results = {i:(0,'primary','original') for i in range(1,12)}
    w.effects['key'] = 1
    assert w.accepted()
    w.goal_version = 1
    assert not w.accepted()
    w.goal_version = 0
    w.effects['key'] = 2
    assert not w.accepted()


def test_experiment_all_strategies_and_complete_failure_denominator():
    policy = yaml.safe_load((EXAMPLE/'checkpoint_policy.yaml').read_text())
    tasks = fixtures()
    checked_in = [json.loads(s) for s in (EXAMPLE/'task_chains.jsonl').read_text().splitlines()]
    assert checked_in == json.loads(json.dumps(tasks))
    assert len(tasks)==10 and all(len(t['nodes'])==11 for t in tasks)
    rows = []
    for strategy in ['fixed','global','local','unsafe_full']:
        for task in tasks:
            row,traces,logs = run(task,strategy,policy)
            rows.append(row)
            assert row['remaining_budget'] >= 0
            assert row['elapsed_ticks'] + row['remaining_budget'] == task['initial_ticks']
            assert traces[-1]['event']=='terminal'
            if strategy in {'global','local'} and task['recoverable']:
                assert row['success'] and row['duplicate_effects']==0
                assert len(logs)==1 and logs[0]['kept_completed']==['n1','n2','n3','n4','n5']
                assert logs[0]['observation']['id']
            if task['kind']=='stall':
                assert row['status']=='STOP_NO_PROGRESS'
            if task['kind']=='unknown_outcome':
                assert row['status']=='ESCALATE'
    assert sum(r['success'] for r in rows if r['strategy']=='local')==8
    assert sum(r['success'] for r in rows if r['strategy']=='fixed')==0
    assert sum(r['duplicate_effects'] for r in rows if r['strategy']=='unsafe_full')==8
    # A pure chain gives identical conservative local/global scopes; no invented advantage.
    for t in tasks:
        g=next(r for r in rows if r['strategy']=='global' and r['task_id']==t['task_id'])
        l=next(r for r in rows if r['strategy']=='local' and r['task_id']==t['task_id'])
        assert {k:v for k,v in g.items() if k!='strategy'} == {k:v for k,v in l.items() if k!='strategy'}
