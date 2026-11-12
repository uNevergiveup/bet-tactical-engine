"""Per-fixture audit trail. A budget deferral is never a failed coverage check."""
from pathlib import Path
import prematch_prospective_trial as trial


class CandidateLedger:
    def __init__(self, path, scope=None):
        self.path = Path(path)
        self.scope = scope or {}
        self.rows = {}

    @classmethod
    def load(cls, path):
        obj = cls(path)
        if Path(path).is_file():
            doc = trial.read(Path(path))
            obj.scope = doc.get('scope', {})
            rows = doc.get('fixtures', [])
            if len({str(r['fixture_id']) for r in rows}) != len(rows):
                raise trial.TrialError('CANDIDATE_LEDGER_DUPLICATE_FIXTURE')
            obj.rows = {str(r['fixture_id']): r for r in rows}
        return obj

    def record(self, fixture, state, stage, reason, evidence=None):
        fid = str(fixture['fixture_id'])
        row = self.rows.setdefault(fid, {k: fixture.get(k) for k in
            ('fixture_id', 'home', 'away', 'kickoff_madrid', 'league', 'league_id','country','season')})
        event={'state': state, 'stage': stage, 'reason': reason, 'evidence': evidence}
        history=row.setdefault('history', [])
        if not history or any(history[-1].get(key)!=value for key,value in event.items()):
            history.append({**event,'at_madrid': trial.now_madrid().isoformat()})
        row['state'] = state
        row['stage'] = stage
        row['reason'] = reason

    def write(self, path=None):
        trial.atomic_dump(Path(path) if path else self.path, {'schema_version': '2.0-candidate-ledger',
            'generated_madrid': trial.now_madrid().isoformat(), 'scope': self.scope,
            'fixtures': list(self.rows.values())})
        from prematch_daily_csv import export_daily
        export_daily(self.path.parent)


def failure_state(reason):
    reason = str(reason).upper()
    if any(x in reason for x in ('IDENTITY', 'MISMATCH', 'CONFLICT', 'TITLE', 'NAME_IDENTITY', 'EVENT_ID')):
        return 'IDENTIDAD_INCORRECTA'
    if any(x in reason for x in ('COVERAGE', 'RESEARCHABLE', 'STATISTICAL', 'CUTOFF')):
        return 'SIN_COBERTURA'
    if any(x in reason for x in ('DISCOVERY_RETRIES', 'NOT_FOUND', 'SIN_BWIN')):
        return 'SIN_BWIN'
    return 'SIN_CUOTAS'
