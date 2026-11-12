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
