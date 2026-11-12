"""LLM research contract over existing discovery statistics; no simulated runtime."""
from collections import defaultdict
import copy
import json
from pathlib import Path

from prematch_advanced_discovery_v2 import split_history, table, by_adjust, digest, dt, finite

SEGMENT = ('entity_type','team_id','player_id','league_id','season','gender','team_kind','period')


def research_packet(observations, targets, cutoff, max_iterations=3):
    if not 1 <= max_iterations <= 10:
        raise ValueError('LLM_ITERATION_LIMIT_INVALID')
    if cutoff.tzinfo is None or any(not dt(t.get('kickoff_madrid')) or dt(t['kickoff_madrid']) <= cutoff for t in targets):
        raise ValueError('LLM_TARGET_CUTOFF_INVALID')
    groups = defaultdict(list)
    for row in observations:
        at = dt(row.get('kickoff_utc')); available = dt(row.get('available_at_utc'))
        acquired = [dt(r.get('fetched_at_utc')) for r in row.get('provenance', [])]
        if not at or not available or available >= cutoff or at >= cutoff:
            raise ValueError('LLM_OBSERVATION_AFTER_CUTOFF')
        if not acquired or any(value is None or value > cutoff for value in acquired):
            raise ValueError('LLM_ACQUISITION_UNVERIFIED_OR_AFTER_CUTOFF')
        groups[tuple(row.get(k) for k in SEGMENT)].append(row)
    training = []; holdout = []
    for key, rows in groups.items():
        ids = [r['fixture_id'] for r in rows]
        if len(set(ids)) != len(ids):
            raise ValueError('LLM_DUPLICATE_SEGMENT_FIXTURE')
        a,b = split_history(rows)
        training.extend(a); holdout.extend(b)
    public = {'schema_version':'1.0-llm-training-packet','cutoff_utc':cutoff.isoformat(),
              'targets':targets,'training_observations':training,'max_iterations':max_iterations,
              'holdout_policy':'SEALED_CHRONOLOGICAL_30_PERCENT_PER_SEGMENT',
              'instructions':'Propose falsifiable associations; cite training fixture IDs; never claim validation or betting value.'}
    sealed = {'training_sha256':digest(public),'holdout':holdout,'all_observations_sha256':digest(observations)}
    return public,sealed


def validate_hypotheses(packet, sealed, proposals, min_group=3):
    """Preserve rejected proposals; training and holdout adjusted across all tests."""
    if digest(packet) != sealed['training_sha256']:
        raise ValueError('LLM_PACKET_BINDING_MISMATCH')
    if min_group < 3:
        raise ValueError('LLM_MINIMUM_GROUP_CANNOT_BE_RELAXED')
    results=[];seen=set()
    for proposal in proposals:
        result={'hypothesis':copy.deepcopy(proposal),'status':'REJECTED','reason':None,
                'predictive_use_authorized':False,'value_validated':False}
        results.append(result)
        identity=proposal.get('hypothesis_id')
        if not identity or identity in seen:
            result['reason']='MISSING_OR_DUPLICATE_HYPOTHESIS_ID';continue
        seen.add(identity)
        if not proposal.get('mechanism') or not proposal.get('falsifier'):
            result['reason']='MECHANISM_AND_FALSIFIER_REQUIRED';continue
        segment=proposal.get('segment',{})
        if set(segment)!=set(SEGMENT):
            result['reason']='EXPLICIT_COMPLETE_SEGMENT_REQUIRED';continue
        train=[r for r in packet['training_observations'] if all(r.get(k)==v for k,v in segment.items())]
        holdout=[r for r in sealed['holdout'] if all(r.get(k)==v for k,v in segment.items())]
        evidence=proposal.get('evidence_fixture_ids')
        if not evidence or not set(evidence)<=set(r['fixture_id'] for r in train):
            result['reason']='EVIDENCE_OUTSIDE_TRAINING_SEGMENT';continue
        condition=proposal.get('condition',{});outcome=proposal.get('outcome');threshold=proposal.get('threshold')
        fields={k for r in train for k in r['metrics']}
        context={k for r in train for k in r['conditions']}
        if (condition.get('operator') not in ('EQ','GT') or 'value' not in condition
            or condition.get('field') not in (context if condition.get('operator')=='EQ' else fields)
            or outcome not in fields or not finite(threshold) or condition.get('field')==outcome):
            result['reason']='UNSUPPORTED_OR_TAUTOLOGICAL_VARIABLES';continue
        if condition['operator']=='GT' and not finite(condition['value']):
            result['reason']='NUMERIC_CONDITION_REQUIRED';continue
        if len(train)<2*min_group or len(holdout)<2*min_group:
            result.update(status='PENDING_INSUFFICIENT_SAMPLE',reason='TRAIN_AND_HOLDOUT_REQUIRE_BOTH_GROUPS')
        else:
            result.update(status='EXPLORATORY',reason='STATISTICAL_TEST_NOT_YET_SUPPORTED')
        result['train']=table(train,condition,outcome,threshold)
        result['holdout']=table(holdout,condition,outcome,threshold)
    tested=[r for r in results if 'train' in r]
    for result,q,qh in zip(tested,by_adjust([r['train']['p_value'] for r in tested]),
                          by_adjust([r['holdout']['p_value'] for r in tested])):
        result.update(training_q=q,holdout_q=qh)
        a,b,c,d=result['train']['counts'];e,f,g,h=result['holdout']['counts']
        if min(a+b,c+d,e+f,g+h)<min_group:
            result.update(status='PENDING_INSUFFICIENT_SAMPLE',reason='CONDITION_OR_REFERENCE_GROUP_TOO_SMALL')
        elif q<=.05 and qh<=.05 and result['train']['risk_difference']*result['holdout']['risk_difference']>0:
            result.update(status='ACCEPTED_TEMPORAL_ASSOCIATION',reason='TRAIN_AND_SEALED_HOLDOUT_SUPPORT_SAME_DIRECTION_NOT_CAUSATION')
    return {'schema_version':'1.0-llm-pattern-validation','results':results,
            'multiple_testing':'BENJAMINI_YEKUTIELI_ALL_STATISTICALLY_TESTABLE_PROPOSALS',
            'runtime_execution_verified_by_this_validator':False,'actionable_bet':False}


def write_pending_runtime(output, packet, base_ranking):
    """Required artifacts are explicit pending records, never invented research."""
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    files={'llm_pattern_hypotheses.json':{'status':'RLM_PENDING','hypotheses':[], 'training_packet_sha256':digest(packet)},
           'pattern_validation.json':{'status':'PENDING_LLM_RUNTIME','results':[]},
           'pattern_registry.json':{'status':'RLM_PENDING','rules':[],'max_iterations':packet['max_iterations']},
           'ranking_enriched.json':{'status':'PENDING_LLM_RESEARCH_NOT_FINAL','base_ranking':base_ranking,'actionable_bet':False}}
    for name,value in files.items():
        with (out/name).open('x') as handle:json.dump(value,handle,ensure_ascii=False,indent=2)
    return out
