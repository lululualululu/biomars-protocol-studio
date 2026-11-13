"""Protect the integrity of the owner's imported historical collection."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'web/examples'


def test_all_fifty_examples_keep_original_hash_and_provenance():
    catalog = json.loads((ROOT / 'index.json').read_text())
    assert catalog['count'] == len(catalog['cases']) == 50
    ids = {case['id'] for case in catalog['cases']}
    assert len(ids) == 50
    assert set(catalog['featured']).issubset(ids)
    for case in catalog['cases']:
        item = json.loads((ROOT / f"{case['id']}.json").read_text())
        assert item['query'] == case['query']
        assert hashlib.sha256(item['markdown'].encode()).hexdigest() == item['provenance']['protocol_sha256']
        assert item['historical'] is True
        assert item['model'] == 'qwen/qwen3.6-flash'
        assert '/Users/' not in json.dumps(item)
    flagged = json.loads((ROOT / 'V2-01.json').read_text())
    assert any('did not pass' in note for note in flagged['review_notes'])
