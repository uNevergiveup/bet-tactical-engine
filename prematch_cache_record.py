"""Acquisition records: filesystem restoration times are never freshness evidence."""
import hashlib
import json
from datetime import datetime, timezone


def payload_sha(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def acquisition_record(payload, now=None):
    return {'schema': 'context-acquisition-v1',
            'fetched_at_utc': (now or datetime.now(timezone.utc)).isoformat(),
            'payload_sha256': payload_sha(payload), 'raw_payload': payload}


def unpack_record(record):
    if record.get('schema') != 'context-acquisition-v1':
        raise ValueError('LEGACY_ACQUISITION_TIME_UNVERIFIED')
    payload = record['raw_payload']
    if payload_sha(payload) != record.get('payload_sha256'):
        raise ValueError('CACHE_PAYLOAD_SHA_MISMATCH')
    acquired = datetime.fromisoformat(record['fetched_at_utc'])
    if acquired.tzinfo is None:
        raise ValueError('CACHE_TIMESTAMP_OFFSET_REQUIRED')
    return payload, acquired


def store_acquisition(directory, payload, now=None):
    """Append a direct response as evidence, including when reusable cache is bypassed."""
    from pathlib import Path
    record = acquisition_record(payload, now)
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode()
    path = Path(directory) / (hashlib.sha256(encoded).hexdigest() + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as handle:
            handle.write(encoded)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise ValueError('ACQUISITION_RECORD_CONFLICT')
    return path


def temporal_stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp if stamp.utcoffset() is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def result_available_at(row):
    """Shared conservative policy, never an invented official completion time."""
    from datetime import timedelta
    for key in ('result_available_at_utc', 'completed_at_utc', 'fixture_completed_at_utc'):
        stamp = temporal_stamp(row.get(key))
        if stamp:
            return stamp, 'EXPLICIT_' + key.upper()
    kickoff = temporal_stamp(row.get('kickoff_utc') or row.get('date_utc') or
                             (row.get('fixture') or {}).get('date'))
    return (kickoff + timedelta(hours=3), 'CONSERVATIVE_KICKOFF_PLUS_3H') if kickoff else (None, 'UNAVAILABLE')


def historical_available(row, cutoff):
    available, _ = result_available_at(row)
    return available is not None and available <= cutoff


def acquisition_before(value, cutoff):
    stamp = temporal_stamp(value)
    return stamp is not None and stamp <= cutoff


def preserve_cache_version(path):
    """Keep exact earlier bytes before replacing a mutable cache pointer."""
    from pathlib import Path
    path=Path(path)
    if not path.is_file():return
    data=path.read_bytes()
    version=path.parent/'versions'/(hashlib.sha256(data).hexdigest()+'.json')
    version.parent.mkdir(parents=True,exist_ok=True)
    try:
        with version.open('xb') as handle:handle.write(data)
    except FileExistsError:
        if version.read_bytes()!=data:raise ValueError('CACHE_VERSION_CONFLICT')
