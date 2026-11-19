"""Report LTCR first-mention matches on comparisons aligned in all four conditions."""

import argparse
from collections import Counter
import json
from pathlib import Path

from localization_agent.selection.checkpoints import write_json


CONDITIONS = ('baseline', 'memory', 'graph', 'selector')


def main(folder, conditions=CONDITIONS):
    details = json.loads((folder / 'details.json').read_text())
    indexed = {}
    for condition in conditions:
        occurrence = Counter()
        rows = {}
        for row in details:
            if row['condition'] != condition:
                continue
            key = (row['source_name'], row['first_source_id'], row['source_id'])
            occurrence[key] += 1
            rows[key + (occurrence[key],)] = row
        indexed[condition] = rows
    keys = [set(rows) for rows in indexed.values()]
    if any(keyset != keys[0] for keyset in keys[1:]):
        raise ValueError('Conditions have different source comparison identities')
    common = set.intersection(*(set(k for k, row in rows.items() if row['status'] == 'compared')
                                for rows in indexed.values()))
    if not common:
        raise ValueError('No comparisons are aligned across all four conditions')
    conditions = {}
    for condition, rows in indexed.items():
        exact = sum(rows[key]['exact'] for key in common)
        fuzzy = sum(rows[key]['fuzzy'] for key in common)
        conditions[condition] = {'ltcr_1': exact / len(common), 'ltcr_1f': fuzzy / len(common),
                                 'exact_matches': exact, 'fuzzy_matches': fuzzy}
    result = {'total_source_comparisons': len(keys[0]), 'common_aligned_comparisons': len(common),
              'conditions': conditions}
    destination = folder / 'common_result.json'
    if destination.exists() and json.loads(destination.read_text()) != result:
        raise ValueError('Existing common-coverage result differs')
    if not destination.exists():
        write_json(destination, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    main(parser.parse_args().output)
