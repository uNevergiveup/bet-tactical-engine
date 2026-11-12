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
