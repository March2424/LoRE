"""Score completed Naev ablation outputs with corpus-level SacreBLEU."""

import argparse
from pathlib import Path
import hashlib
import json

from sacrebleu.metrics import BLEU

from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.preferences import lines


CONDITIONS = ('baseline', 'memory', 'graph', 'selector')
REFERENCES = Path('outputs/cpu_preparation_2026-11-02_2000_context_v2/offline/references.jsonl')


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def score(folder, conditions=CONDITIONS, expected_count=300):
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    target_ids = [row['source_id'] for row in manifest['samples'] if row['phase'] == 'target']
    if len(target_ids) != expected_count or len(set(target_ids)) != expected_count:
        raise ValueError(f'The frozen manifest must contain {expected_count} distinct target IDs')
    targets = set(target_ids)
    references = {}
    for row in lines(REFERENCES):
        key = row['record_id']
        if row['game'] == 'naev' and row['split'] == 'external_test' and key in targets:
            if key in references or not row['reference_text'].strip():
                raise ValueError('Duplicate or empty reference: ' + key)
            references[key] = row['reference_text']
    if references.keys() != targets:
        raise ValueError('The reference set does not match the frozen target IDs')

    metric = BLEU(tokenize='zh', lowercase=False, smooth_method='exp', effective_order=False)
    results = {}
    for condition in conditions:
        state = load_state(folder / condition)
        jobs = [job for job in state['jobs'] if job['phase'] == 'target']
        if len(jobs) != expected_count or any(job['status'] not in ('complete', 'failed') for job in jobs):
            raise ValueError('Condition has unfinished targets: ' + condition)
        by_id = {job['source_id']: job for job in jobs}
        if len(by_id) != expected_count or by_id.keys() != targets:
            raise ValueError('Condition target IDs differ from the frozen manifest: ' + condition)
        hypotheses = []
        for key in target_ids:
            job = by_id[key]
            hypothesis = job.get('eval_reply', {}).get('text', '')
            if not isinstance(hypothesis, str) or (job['status'] == 'complete' and not hypothesis.strip()):
                raise ValueError('Completed target has no valid hypothesis: ' + key)
            hypotheses.append(hypothesis)
        result = metric.corpus_score(hypotheses, [[references[key] for key in target_ids]])
        results[condition] = {
            'bleu4': result.score,
            'n': len(hypotheses),
            'failed': sum(job['status'] == 'failed' for job in jobs),
            'empty_hypotheses': sum(not text.strip() for text in hypotheses),
            'precisions': result.precisions,
            'brevity_penalty': result.bp,
            'hypothesis_tokens': result.sys_len,
            'reference_tokens': result.ref_len,
        }
    return {
        'metric': 'corpus BLEU-4',
        'scale': '0-100',
        'signature': str(metric.get_signature()),
        'manifest_sha256': sha256(manifest_path),
        'references_sha256': sha256(REFERENCES),
        'hypothesis_policy': 'Use every stored hypothesis, including failed jobs; missing failed hypotheses are empty strings.',
        'conditions': results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Prepared ablation directory')
    args = parser.parse_args()
    result = score(args.output)
    destination = args.output / 'bleu4_result.json'
    if destination.exists():
        if json.loads(destination.read_text()) != result:
            raise ValueError('Existing BLEU result differs; preserve it and inspect the inputs')
    else:
        write_json(destination, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
