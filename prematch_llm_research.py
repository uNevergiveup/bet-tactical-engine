"""LLM research contract over existing discovery statistics; no simulated runtime."""
from collections import defaultdict
import copy
import json
from pathlib import Path

from prematch_advanced_discovery_v2 import table, by_adjust, digest, dt, finite

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
    # One global boundary prevents a fixture held out for a player being exposed
    # through its team or opponent in the LLM's training packet.
    fixtures = {}
    for row in observations:
        fid = row['fixture_id']; at = dt(row['kickoff_utc'])
        if fid in fixtures and fixtures[fid] != at:
            raise ValueError('LLM_FIXTURE_TIME_CONFLICT')
        fixtures[fid] = at
    ordered = sorted(fixtures, key=lambda fid: (fixtures[fid], fid))
    boundary = fixtures[ordered[int(len(ordered)*.7)]] if ordered else cutoff
    training = []; holdout = []
    for key, rows in groups.items():
        ids = [r['fixture_id'] for r in rows]
        if len(set(ids)) != len(ids):
            raise ValueError('LLM_DUPLICATE_SEGMENT_FIXTURE')
        a = [r for r in rows if dt(r['kickoff_utc']) < boundary]
        b = [r for r in rows if dt(r['kickoff_utc']) >= boundary]
        training.extend(a); holdout.extend(b)
    public = {'schema_version':'1.0-llm-training-packet','cutoff_utc':cutoff.isoformat(),
              'targets':targets,'training_observations':training,'max_iterations':max_iterations,
              'holdout_policy':'SEALED_GLOBAL_CHRONOLOGICAL_30_PERCENT_UNIQUE_FIXTURES',
              'holdout_boundary_utc':boundary.isoformat(),
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


def hypothesis_signature(proposal):
    return digest({k:proposal.get(k) for k in ('segment','condition','outcome','threshold')})


def run_iterative_research(packet, sealed, base_ranking, researcher, runtime_identity):
    """Run a real caller-supplied LLM adapter; never expose holdout feedback.

    researcher(public_request) returns hypotheses plus a raw runtime receipt.
    A callable is required: recorded synthetic examples are not a fallback.
    All proposals, including duplicates and invalid ones, survive in the registry.
    Statistical validation happens ONCE after the bounded exploration is closed.
    """
    if not callable(researcher) or not runtime_identity:
        raise ValueError('REAL_LLM_ADAPTER_AND_IDENTITY_REQUIRED')
    if digest(packet) != sealed['training_sha256']:
        raise ValueError('LLM_PACKET_BINDING_MISMATCH')
    target_ids = [t['fixture_id'] for t in packet['targets']]
    ranking_ids = [r['fixture_id'] for r in base_ranking]
    if len(set(ranking_ids)) != len(ranking_ids) or set(ranking_ids) != set(target_ids):
        raise ValueError('LLM_RANKING_MUST_RETAIN_ALL_TARGETS')
    proposals=[]; iterations=[]; seen=set(); stop='ITERATION_LIMIT'
    for number in range(1, packet['max_iterations']+1):
        request={'packet':copy.deepcopy(packet),'iteration':number,
                 'previous_hypotheses':copy.deepcopy(proposals),
                 'feedback_policy':'NO_HOLDOUT_OR_VALIDATION_FEEDBACK_UNTIL_FINALIZATION'}
        response=researcher(request)
        if not isinstance(response,dict) or not isinstance(response.get('hypotheses'),list) or not response.get('runtime_receipt'):
            raise ValueError('LLM_RESPONSE_OR_RUNTIME_RECEIPT_MISSING')
        fresh=0; testable=0
        for original in response['hypotheses']:
            if not isinstance(original,dict):
                raise ValueError('LLM_HYPOTHESIS_MUST_BE_OBJECT')
            proposal=copy.deepcopy(original);proposal['iteration']=number
            signature=hypothesis_signature(proposal)
            proposal['novel_variables']=signature not in seen
            seen.add(signature);proposals.append(proposal)
            if not proposal['novel_variables']:continue
            fresh+=1
            segment=proposal.get('segment',{})
            if set(segment)!=set(SEGMENT):continue
            train=[r for r in packet['training_observations'] if all(r.get(k)==v for k,v in segment.items())]
            condition=proposal.get('condition',{})
            if (condition.get('operator') not in ('EQ','GT') or 'value' not in condition
                or not condition.get('field') or condition.get('field')==proposal.get('outcome')
                or not finite(proposal.get('threshold')) or not proposal.get('evidence_fixture_ids')
                or not set(proposal['evidence_fixture_ids']) <= {r['fixture_id'] for r in train}
                or (condition['operator']=='GT' and not finite(condition['value']))):continue
            # This gate sees only training sample counts, never holdout outcomes.
            stats=table(train,condition,proposal.get('outcome'),proposal['threshold'])
            a,b,c,d=stats['counts']
            if min(a+b,c+d)>=3:testable+=1
        iterations.append({'iteration':number,'request_sha256':digest(request),
                           'runtime_receipt':copy.deepcopy(response['runtime_receipt']),
                           'response_sha256':digest(response),'proposals':len(response['hypotheses']),
                           'novel_proposals':fresh,'novel_training_testable':testable})
        if not fresh:stop='NO_NEW_RELATION';break
        if not testable:stop='NO_NEW_TRAINING_TESTABLE_RELATION';break
        if not response.get('continue_research',False):stop='RUNTIME_RESEARCH_COMPLETE';break
    # Duplicate variables are not extra tests and cannot masquerade as novelty.
    unique=[]; signatures=set(); duplicate_results=[]
    for proposal in proposals:
        signature=hypothesis_signature(proposal)
        if signature in signatures:
            duplicate_results.append({'hypothesis':proposal,'status':'REJECTED',
                'reason':'DUPLICATE_VARIABLES','predictive_use_authorized':False,'value_validated':False})
        else:signatures.add(signature);unique.append(proposal)
    validation=validate_hypotheses(packet,sealed,unique)
    validation['results'].extend(duplicate_results)
    enriched=[]
    for row in base_ranking:
        item=copy.deepcopy(row)
        target=next(t for t in packet['targets'] if t['fixture_id']==row['fixture_id'])
        teams={t for t in (target.get('home_id'),target.get('away_id')) if t is not None}
        related=[r for r in validation['results'] if r['hypothesis'].get('segment',{}).get('team_id') in teams]
        item['llm_evidence']=[{'hypothesis_id':r['hypothesis'].get('hypothesis_id'),
            'status':r['status'],'reason':r['reason']} for r in related]
        item['llm_ranking_policy']='EVIDENCE_ANNOTATION_ONLY_BASE_ORDER_PRESERVED'
        item['value_validated']=False;item['actionable_bet']=False;enriched.append(item)
    common={'runtime_identity':runtime_identity,'training_packet_sha256':digest(packet),
            'status':'RLM_EXECUTED','actionable_bet':False}
    return {'llm_pattern_hypotheses.json':dict(common,hypotheses=proposals,iterations=iterations),
            'pattern_validation.json':validation,
            'pattern_registry.json':dict(common,rules=validation['results'],max_iterations=packet['max_iterations'],
                iterations_executed=len(iterations),stop_reason=stop,
                holdout_feedback_policy='SEALED_UNTIL_ALL_PROPOSALS_FROZEN'),
            'ranking_enriched.json':dict(common,status='RESEARCH_ENRICHED_UNCALIBRATED',ranking=enriched)}


def write_research_artifacts(output, artifacts):
    out=Path(output)
    # Require a fresh result directory; no prior iteration or source is overwritten.
    out.mkdir(parents=True,exist_ok=False)
    for name,document in artifacts.items():
        if name not in {'llm_pattern_hypotheses.json','pattern_validation.json','pattern_registry.json','ranking_enriched.json'}:
            raise ValueError('UNEXPECTED_LLM_ARTIFACT')
        with (out/name).open('x') as handle:
            json.dump(document,handle,ensure_ascii=False,indent=2,allow_nan=False)
    return out


def main(argv=None):
    """JSON stdin/stdout adapter for a real installed LLM/RLM runtime."""
    import argparse
    import shutil
    import subprocess
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observations',type=Path,required=True)
    parser.add_argument('--base-ranking',type=Path,required=True,help='JSON list of all fixture rows')
    parser.add_argument('--cutoff',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--max-iterations',type=int,default=3)
    parser.add_argument('--runtime-command-json',help='JSON argv array; no shell; reads request JSON on stdin')
    parser.add_argument('--runtime-timeout',type=int,default=180)
    args=parser.parse_args(argv)
    if args.runtime_timeout < 1:parser.error('runtime timeout must be positive')
    data=json.loads(args.observations.read_text());ranking=json.loads(args.base_ranking.read_text())
    cutoff=dt(args.cutoff)
    if cutoff is None:parser.error('cutoff must include a timezone')
    packet,sealed=research_packet(data['observations'],data['targets'],cutoff,args.max_iterations)
    command=json.loads(args.runtime_command_json) if args.runtime_command_json else None
    if command is not None and (not isinstance(command,list) or not command or any(not isinstance(x,str) for x in command)):
        parser.error('runtime command must be a nonempty JSON string array')
    if args.output_dir.exists():raise ValueError('LLM_OUTPUT_DIRECTORY_ALREADY_EXISTS')
    if not command or not shutil.which(command[0]):
        write_pending_runtime(args.output_dir,packet,ranking)
        dependency={'status':'RLM_PENDING','reason':'RUNTIME_NOT_CONFIGURED' if not command else 'EXECUTABLE_UNAVAILABLE',
                    'executable':command[0] if command else None,'protocol':'JSON_STDIN_STDOUT',
                    'max_iterations':args.max_iterations,'holdout_not_sent':True}
        with (args.output_dir/'runtime_dependency.json').open('x') as handle:json.dump(dependency,handle,indent=2)
        print(json.dumps(dependency));return 2
    def adapter(request):
        result=subprocess.run(command,input=json.dumps(request,ensure_ascii=False),text=True,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              timeout=args.runtime_timeout,check=False)
        if result.returncode:raise RuntimeError('LLM_RUNTIME_FAILED_EXIT_'+str(result.returncode))
        response=json.loads(result.stdout)
        response['runtime_receipt']={'executable':shutil.which(command[0]),
            'command_sha256':digest(command),'response_sha256':digest(response),
            'exit_code':result.returncode,'protocol':'JSON_STDIN_STDOUT'}
        return response
    artifacts=run_iterative_research(packet,sealed,ranking,adapter,{'kind':'EXTERNAL_COMMAND','executable':shutil.which(command[0])})
    write_research_artifacts(args.output_dir,artifacts)
    print(json.dumps({'status':'RLM_EXECUTED','output_dir':str(args.output_dir.resolve()),
                      'iterations':artifacts['pattern_registry.json']['iterations_executed'],'actionable_bet':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
