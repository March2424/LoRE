"""Prepare/manual-run DocMTAgent DelTA additions without changing old experiments."""
import argparse
from collections import Counter
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from delta_runtime import CachedCalls, DeltaMemory, LocalQwen, digest, read_json, write_json

DEFAULT_CONFIG = Path('configs/delta_eval.json')
CODE = ('scripts/delta_runtime.py', 'scripts/run_delta_eval.py')


def frozen_files(config):
    root = Path(config['upstream'])
    paths = [root / 'infer/infer_qwen.py', root / 'LICENSE', *root.glob('prompts/**/*.txt'),
             *Path('prompts/delta').glob('*.txt'), *(Path(p) for p in CODE)]
    return {str(p): digest(p) for p in paths}


def model_assets(config):
    result = {}
    for name, location in config['models'].items():
        path = Path(location)
        shards = {p.name: p.stat().st_size for p in sorted(path.glob('*.safetensors'))}
        if not shards:
            raise ValueError('Missing Qwen weight shards: ' + name)
        result[name] = dict(config=digest(path / 'config.json'),
                            tokenizer=digest(path / 'tokenizer_config.json'), shards=shards)
    return result


def source_payload(db_path, project, key):
    with sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload FROM sources WHERE project=? AND id=? ORDER BY version DESC LIMIT 1',
                         (project, key)).fetchone()
    if not row:
        raise ValueError('Missing source occurrence: ' + key)
    return json.loads(row[0])


def prepare(config_path):
    config = read_json(config_path)
    output = Path(config['output'])
    if output.exists():
        raise ValueError('Output exists; preserve frozen plan: ' + str(output))
    commit = subprocess.check_output(['git', '-C', config['upstream'], 'rev-parse', 'HEAD'], text=True).strip()
    if commit != config['upstream_commit']:
        raise ValueError('Unexpected upstream commit')
    if subprocess.check_output(['git', '-C', config['upstream'], 'status', '--porcelain'], text=True).strip():
        raise ValueError('Keep upstream checkout unmodified; adaptation belongs in our wrapper')
    naev_path = Path(config['naev_prior']) / 'manifest.json'
    compare_path = Path(config['naev_compare_prior']) / 'manifest.json'
    general_path = Path(config['general_prior']) / 'manifest.json'
    naev, compare, general = map(read_json, (naev_path, compare_path, general_path))
    warm = [s for s in naev['samples'] if s['phase'] == 'warmup']
    target = [s for s in naev['samples'] if s['phase'] == 'target']
    if len(warm) != 10 or len(target) != 300:
        raise ValueError('Expected frozen Naev 10 + 300')
    if [r['source_id'] for r in compare['samples']] != [r['source_id'] for r in warm + target[:100]]:
        raise ValueError('100-sample comparison is not the identical warmup/target prefix')
    if digest(config['naev_graph']) != naev['graph_sha256']:
        raise ValueError('Naev source graph differs')
    for spec in general['graphs'].values():
        if digest(spec['path']) != spec['sha256']:
            raise ValueError('General source graph differs')
    inputs = {}
    for model in config['models']:
        rows = []
        for row in warm + (target if model == '14b' else target[:100]):
            source = source_payload(config['naev_graph'], 'naev', row['source_id'])
            rows.append(dict(row, source=source['source'], source_version=source['source_version'],
                source_lang='en', target_lang='zh',
                metadata=source['metadata'], mandatory_rules=[], unit_id='naev_processing_schedule',
                dataset='naev', game='naev', split='external_test'))
        inputs[model + '/naev'] = rows
        for dataset in ('news', 'guofeng'):
            rows = []
            for row in (r for r in general['samples'] if r['dataset'] == dataset):
                source = source_payload(general['graphs'][row['project_id']]['path'], row['project_id'], row['source_id'])
                if digest_text(source['source']) != row['source_hash']:
                    raise ValueError('Source content changed')
                # Sentence/Doc2Doc comparators receive no additional metadata or project rules.
                rows.append(dict(row, source=source['source'], metadata={}, mandatory_rules=[],
                                 phase='target', game=row['project_id'], group_id=row['unit_id']))
            if len(rows) != (100 if dataset == 'news' else 99):
                raise ValueError('Unexpected frozen general-domain sample count')
            inputs[model + '/' + dataset] = rows
    # No graph retrieval, approved memories or reference fields enter these source-only files.
    assets = model_assets(config)
    output.mkdir(parents=True)
    manifests = {str(p): digest(p) for p in (naev_path, compare_path, general_path)}
    write_json(output / 'plan.json', dict(config=config, source_manifests=manifests,
        files=frozen_files(config), upstream_commit=commit, model_assets=assets,
        protocol='DocMTAgent classes + documented runtime fixes; same Qwen for all calls',
        naev_14b_prefix_reuse=True, guofeng='selector-training seen', runs=list(inputs)))
    for name, rows in inputs.items():
        path = output / 'runs' / name
        write_json(path / 'inputs.json', rows)
        jobs = [dict(source_id=r['source_id'], phase=r['phase'], game=r['game'],
                     group_id=r['group_id'], unit_id=r['unit_id'], status='pending') for r in rows]
        write_json(path / 'state.json', dict(status='prepared', jobs=jobs))
    plan = read_json(output / 'plan.json')
    plan['inputs'] = {name: digest(output / 'runs' / name / 'inputs.json') for name in inputs}
    write_json(output / 'plan.json', plan)
    print('Prepared 9 DelTA runs: Naev 14B 300, 7B/8B 100; 3 models x News 100/GuoFeng 99. No model loaded.')


def digest_text(text):
    from hashlib import sha256
    return sha256(text.encode()).hexdigest()


def verify(output):
    plan = read_json(output / 'plan.json')
    for p, expected in {**plan['source_manifests'], **plan['files']}.items():
        if digest(p) != expected:
            raise ValueError('Frozen file changed: ' + p)
    for name, expected in plan['inputs'].items():
        if digest(output / 'runs' / name / 'inputs.json') != expected:
            raise ValueError('Frozen source-only inputs changed: ' + name)
    if model_assets(plan['config']) != plan['model_assets']:
        raise ValueError('Frozen model assets changed')
    return plan


def execute_rows(path, rows, config, backend):
    state = read_json(path / 'state.json')
    if [j['source_id'] for j in state['jobs']] != [r['source_id'] for r in rows]:
        raise ValueError('Checkpoint IDs/order differ from input manifest')
    state.update(status='running', pid=os.getpid())
    write_json(path / 'state.json', state)
    memory, unit = None, None
    for i, (row, job) in enumerate(zip(rows, state['jobs'])):
        if (path / 'STOP').exists():
            state['status'] = 'stopped'
            write_json(path / 'state.json', state)
            return False
        calls = CachedCalls(path / 'calls' / f'{i:06d}', backend)
        if row['unit_id'] != unit:
            memory = DeltaMemory(config, row['source_lang'], row['target_lang'],
                                 'game' if row['dataset'] == 'naev' else 'general', calls)
            unit = row['unit_id']
        memory.call = calls
        checkpoint = path / 'jobs' / f'{i:06d}.json'
        if checkpoint.exists() and read_json(checkpoint)['status'] == 'complete':
            detail = read_json(checkpoint)
            memory.restore(detail['memory_after'])
            job.update(status='complete', eval_reply=detail['eval_reply'])
            continue
        job['status'] = 'working'
        state['current_source_id'] = row['source_id']
        write_json(path / 'state.json', state)
        start = time.monotonic()
        try:
            text, trace = memory.translate(row)
            reply = next(c['reply'] for c in calls.replies if c['task'] == 'translate')
            detail = dict(job, status='complete', eval_reply=reply, **trace,
                          elapsed_seconds=time.monotonic() - start, memory_after=memory.snapshot())
            write_json(checkpoint, detail)
            job.update(status='complete', eval_reply=reply,
                       retrieval_fallback=any('fallback' in s for s in trace['diagnostics']))
        except Exception as error:
            job.update(status='needs_attention', error=type(error).__name__ + ': ' + str(error))
            state['status'] = 'needs_attention'
            write_json(path / 'state.json', state)
            raise
        write_json(path / 'state.json', state)
        print(f'{path.name} {i+1}/{len(rows)} {row["phase"]} {row["source_id"]}', flush=True)
    state.update(status='complete', current_source_id=None)
    write_json(path / 'state.json', state)
    entries = [read_json(p) for p in sorted((path / 'calls').glob('*/*.json'))]
    write_json(path / 'run_summary.json', dict(
        calls=len(entries), input_tokens=sum(e['reply'].get('input_tokens') or 0 for e in entries),
        output_tokens=sum(e['reply'].get('output_tokens') or 0 for e in entries),
        retrieval_fallback=sum(bool(j.get('retrieval_fallback')) for j in state['jobs']),
        cost=sum(e['reply'].get('cost') or 0 for e in entries),
        unpriced_calls=sum(e['reply'].get('cost') is None for e in entries),
        cost_note='local inference API cost=0; GPU rental not included; unpriced API calls are not free'))
    return True


def run(output, model):
    plan = verify(output)
    config = plan['config']
    launch = output / 'runs' / model / 'launcher.json'
    with (launch.parent / '.launcher.lock').open('a') as own:
        fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
        write_json(launch, dict(status='waiting_for_gpu', pid=os.getpid()))
        lock = Path(config['gpu_lock'])
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a') as gpu:
            fcntl.flock(gpu, fcntl.LOCK_EX)
            try:
                write_json(launch, dict(status='running', pid=os.getpid()))
                for dataset in ('naev', 'news', 'guofeng'):
                    path = output / 'runs' / model / dataset
                    if read_json(path / 'state.json')['status'] == 'complete':
                        continue
                    subprocess.run([sys.executable, __file__, 'worker', '--output', str(output),
                                    '--model', model, '--dataset', dataset], check=True)
                write_json(launch, dict(status='complete', pid=os.getpid()))
            except Exception as error:
                write_json(launch, dict(status='needs_attention', pid=os.getpid(), error=str(error)))
                raise


def progress(output):
    plan = read_json(output / 'plan.json')
    for name in plan['runs']:
        state = read_json(output / 'runs' / name / 'state.json')
        counts = Counter((j['phase'], j['status']) for j in state['jobs'])
        total = sum(j['phase'] == 'target' for j in state['jobs'])
        print(f'{name:12} target {counts["target", "complete"]:3}/{total} '
              f'warmup {counts["warmup", "complete"]:2} {state["status"]} '
              f'retrieval_fallback={sum(bool(j.get("retrieval_fallback")) for j in state["jobs"])} '
              f'{state.get("current_source_id") or ""}', flush=True)
    for model in plan['config']['models']:
        p = output / 'runs' / model / 'launcher.json'
        if p.exists():
            print(model, read_json(p), flush=True)


def smoke(config_path, output):
    """Explicit optional DeepSeek transport validation using synthetic text only."""
    from dataclasses import asdict
    from localization_agent.models.chat_completions import CallBudget, ChatCompletionsModel
    config = read_json(config_path)
    config = dict(config, summary_step=2)
    client = ChatCompletionsModel(endpoint='https://api.deepseek.com/chat/completions',
        model='deepseek-chat', max_output_tokens=512, context_tokens=16384, temperature=0,
        budget=CallBudget(max_calls=30, max_tokens=50000))

    class API:
        remote = True
        def __call__(self, task, prompt):
            return asdict(client(task, prompt))

    # Five synthetic resources exercise retrieval, summary creation/merge, entity history and multiline output.
    texts = ['Mira reached Pine Harbor.\n"The gate is open," she said.',
             'At Pine Harbor, Mira repaired a blue boat.',
             'Mira asked Theo to bring the boat to Pine Harbor.',
             'Theo waited by the harbor.\n"Mira will arrive soon," he said.',
             'Mira returned to Pine Harbor with Theo.']
    output.mkdir(parents=True, exist_ok=False)
    rows = [dict(source_id=f'synthetic_{i}', source=s, phase='target', dataset='naev',
                 unit_id='synthetic', source_lang='en', target_lang='zh') for i, s in enumerate(texts)]
    write_json(output / 'state.json', dict(status='prepared', jobs=[dict(source_id=r['source_id'],
        phase='target', status='pending') for r in rows]))
    execute_rows(output, rows, config, API())
    write_json(output / 'validation.json', dict(real_experiment=False, model='deepseek-chat',
        data='synthetic only', summary_step=2, budget=client.budget.snapshot()))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=('prepare', 'verify', 'run', 'worker', 'monitor', 'smoke'))
    p.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    p.add_argument('--output', type=Path)
    p.add_argument('--model', choices=('7b', '8b', '14b'))
    p.add_argument('--dataset', choices=('naev', 'news', 'guofeng'))
    p.add_argument('--interval', type=float, default=5)
    a = p.parse_args()
    output = a.output or Path(read_json(a.config)['output'])
    if a.stage == 'prepare':
        prepare(a.config)
    elif a.stage == 'verify':
        verify(output); print('Frozen inputs and code verified; no model/API loaded.')
    elif a.stage == 'smoke':
        if not a.output: p.error('smoke requires a separate --output')
        smoke(a.config, a.output)
    elif a.stage == 'monitor':
        while True:
            progress(output)
            if a.interval <= 0: break
            time.sleep(a.interval)
    else:
        if not a.model: p.error('--model is required')
        if a.stage == 'run': run(output, a.model)
        else:
            if not a.dataset: p.error('--dataset is required')
            config = verify(output)['config']
            path = output / 'runs' / a.model / a.dataset
            # Workers are launched under the parent's shared GPU lock.
            ok = execute_rows(path, read_json(path / 'inputs.json'), config,
                              LocalQwen(config['models'][a.model], config))
            if not ok: raise SystemExit(2)
