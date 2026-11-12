"""Frozen count-contract research. No network, picks or automatic calibration claims."""
import math
from statistics import mean, variance
from prematch_market_mapping_v1 import norm, market_period, parse_line, exact_owner_alias

def contract(row, target):
    name=row['market'];text=norm(name);line=parse_line(row['selection'])
    base={'market':name,'selection':row['selection'],'odd':float(row['odd']),
          'period':market_period(name),'line':line,'scope':None,'metric':None,
          'status':'UNSUPPORTED_CONTRACT'}
    if any(x in text for x in ('handicap','resultado','ambos',' y ','marcador','crea tus','descanso/')):return base
    if not line or base['period'] not in ('FULL_TIME','FIRST_HALF','SECOND_HALF'):return base
    # Only literal total count contracts. Player identity needs a player-ID roster.
    for token,metric in [('total de goles','goals'),('total de corners','corners'),('total de tarjetas','yellow'),('total de faltas','fouls'),('total de remates','shots')]:
        if token in text:base['metric']=metric;break
    if base['metric'] is None:return base
    if base['metric']=='yellow':
        base['status']='CARD_SETTLEMENT_REQUIRES_REVIEW';return base
    prefix=name.split(' - ',1)[0] if ' - ' in name else None
    if prefix:
        from generate_context import team_name_similarity
        owners=[side for side in ('home','away') if exact_owner_alias(prefix,target[side]) or team_name_similarity(prefix,target[side],'female' if ' W' in target[side] else None)==100]
        if len(owners)!=1:base['status']='OWNER_UNRESOLVED';return base
        base.update(scope='TEAM',team_id=target[owners[0]+'_id'])
    else:base.update(scope='MATCH',team_id=None)
    base['status']='EXACT_COUNT_CONTRACT'
    return base

def eligible(rows,target,metric,period,team_id=None):
    suffix={'FULL_TIME':'','FIRST_HALF':'_h1','SECOND_HALF':'_h2'}[period]
    own='own.'+metric+suffix;against='conceded.'+metric+suffix
    selected={}
    for r in rows:
        if r['entity_type']!='TEAM' or r['team_id'] not in (target['home_id'],target['away_id']):continue
        if (r['league_id'],r['season'],r['gender'])!=(target['league_id'],target['season'],target['gender']):continue
        from prematch_market_mapping_v1 import dt
        if dt(r['available_at_utc'])>=dt(target['kickoff_madrid']):continue
        if team_id is not None and r['team_id']!=team_id:continue
        a=r['metrics'].get(own);b=r['metrics'].get(against)
        if not isinstance(a,(int,float)) or isinstance(a,bool):continue
        if team_id is None and (not isinstance(b,(int,float)) or isinstance(b,bool)):continue
        value=a if team_id is not None else a+b
        if not math.isfinite(value) or value<0 or int(value)!=value:continue
        key=r['fixture_id'];entry={'fixture_id':key,'kickoff':r['kickoff_utc'],'available':r['available_at_utc'],'value':value}
        if key in selected and selected[key]['value']!=value:raise ValueError('INCONSISTENT_JOINT_TOTAL')
        selected[key]=entry
    return sorted(selected.values(),key=lambda r:r['kickoff'])

def estimate(values,line):
    # Half-integer contracts have no push. Integer/quarter Asian settlement is blocked.
    if line['line']%1!=.5:return {'status':'SETTLEMENT_MODEL_UNAVAILABLE','probability':None}
    n=len(values)
    if n<3:return {'status':'INSUFFICIENT_SAMPLE','probability':None,'n':n}
    wins=sum(v>line['line'] if line['direction']=='OVER' else v<line['line'] for v in values)
    p=(wins+.5)/(n+1)
    z=1.96;den=1+z*z/n;center=(wins/n+z*z/(2*n))/den
    width=z*math.sqrt((wins/n)*(1-wins/n)/n+z*z/(4*n*n))/den
    return {'status':'MODEL_IMPLEMENTED_UNCALIBRATED','probability':None,
        'exploratory_probability':p,'interval_wilson':[max(0,center-width),min(1,center+width)],
        'n':n,'mean':mean(values),'variance':variance(values),
        'overdispersion_ratio':variance(values)/mean(values) if mean(values)>0 else None,
        'model':'JEFFREYS_EMPIRICAL_COUNT_CDF','assumptions':['Exchangeable comparable historical counts','No causal interpretation'],
        'parameters':{'successes':wins,'alpha':.5,'beta':.5},'calibrated':False}

def validate(history,line):
    predictions=[]
    from prematch_market_mapping_v1 import dt
    for row in history:
        past=[r['value'] for r in history if dt(r['available'])<dt(row['kickoff'])]
        fit=estimate(past,line)
        if fit.get('exploratory_probability') is None:continue
        p=fit['exploratory_probability'];y=int(row['value']>line['line'] if line['direction']=='OVER' else row['value']<line['line'])
        predictions.append({'fixture_id':row['fixture_id'],'p':p,'y':y,'training_n':len(past)})
    if not predictions:return {'status':'NO_TEMPORAL_VALIDATION_SAMPLE','n':0,'calibrated':False}
    brier=mean((r['p']-r['y'])**2 for r in predictions)
    return {'status':'PREQUENTIAL_EXPLORATORY_NOT_CALIBRATED','n':len(predictions),'brier':brier,
        'log_loss':mean(-math.log(r['p'] if r['y'] else 1-r['p']) for r in predictions),
        'baseline_brier_constant_half':.25,'calibration_gap':mean(r['p'] for r in predictions)-mean(r['y'] for r in predictions),
        'predictions':predictions,'calibrated':False,'limitations':['No independent prospective calibration cohort','Repeated targets and correlated observations are not independent trials']}

def prematch_features(pattern):
    field=pattern['variables']['condition']['field']
    kind=('PREMATCH_OBSERVABLE' if field=='venue' else 'HISTORICAL_AGGREGATE_REQUIRED' if field.startswith(('lag.','context.')) else
          'LINEUP_DEPENDENT' if field in ('starter','position','formation','coach_id') else 'POSTMATCH_ONLY')
    return {'field':field,'classification':kind,'authorized_predictive_use':False,
        'counterevidence_fixture_ids':pattern['counterevidence_fixture_ids'],'transform_fitted_on_holdout':False}
