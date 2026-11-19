"""Run blinded Loong-style Responses API evaluation on a frozen result export."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import random
from statistics import mean
from string import Template
import threading
import time

MODEL = 'deepseek-v4.1-flash-1017'
ENDPOINT = 'https://tokendance.space/gateway/v1'
CORE = ('general_quality', 'cohesion', 'coherence', 'style_consistency', 'terminology_consistency')
GAME = ('character_relations', 'localization_constraints')
TEMPLATES = {'news_commentary': 'news_en_zh', 'guofeng': 'guofeng_zh_en',
             'naev': 'naev_en_zh', 'naev_ablation_300': 'naev_en_zh'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def position(row):
    return row['source_id'], row['document_id'], row['line']


def prepare(root, output, prompts):
    specs = read(root / 'data/test_manifest.json')['datasets']
    result_manifest = root / 'results/manifest.json'
    index = read(result_manifest)['conditions']
    if len(index) != 38 or len({c['path'] for c in index}) != 38:
        raise ValueError('Expected 38 distinct conditions in results/manifest.json')
    files = {str(result_manifest.relative_to(root)): digest(result_manifest),
             'data/test_manifest.json': digest(root / 'data/test_manifest.json')}
    score_summary = root / 'evaluate/summary.json'
    source_note = read(score_summary).get('hypothesis_source') if score_summary.is_file() else None
    data = {}
    for name, spec in specs.items():
        path = root / 'data' / name / spec['direction'] / 'records.json'
        if digest(path) != spec['files_sha256']['records.json']:
            raise ValueError('Frozen test data changed for ' + name)
        files[str(path.relative_to(root))] = digest(path)
        data[name] = read(path)

    tasks = []
    output.mkdir(parents=True, exist_ok=False)
    copied_prompts = {}
    for name in sorted(set(TEMPLATES.values())):
        origin = prompts / (name + '.txt')
        content = origin.read_text(encoding='utf-8')
        path = output / 'prompts' / (name + '.txt')
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding='utf-8')
        copied_prompts[name] = digest(path)
    for condition in index:
        name = condition['path'].split('/')[0]
        result_dir = root / 'results' / condition['path']
        results = read(result_dir / 'records.json')
        score_path = result_dir / 'scores.json'
        files[str((result_dir / 'records.json').relative_to(root))] = digest(result_dir / 'records.json')
        files[str(score_path.relative_to(root))] = digest(score_path)
        if [position(row) for row in data[name]] != [position(row) for row in results]:
            raise ValueError('Source and hypothesis positions differ: ' + condition['path'])
        if len(results) != condition['n'] or len({row['source_id'] for row in results}) != len(results):
            raise ValueError('Wrong row count or duplicate IDs in ' + condition['path'])
        units_by_batch = defaultdict(list)
        for i, (source, hyp) in enumerate(zip(data[name], results)):
            batch = f'batch_{i // 10 + 1:02d}' if name.startswith('naev') else source['document_id']
            units_by_batch[batch].append((source, hyp))
        for batch, rows in units_by_batch.items():
            task_id = f'j{len(tasks) + 1:03d}'
            units = [
                dict(id=f'u{i:03d}', source=source['src'], reference=source['ref'], hypothesis=hyp['hyp'])
                for i, (source, hyp) in enumerate(rows, 1)
            ]
            req = dict(task_id=task_id, dataset=name, experiment=condition['experiment'],
                       condition=condition['condition'], condition_path=condition['path'], batch=batch,
                       template=TEMPLATES[name], units=units, mapping={
                           unit['id']: dict(source_id=source['source_id'], document_id=source['document_id'],
                                            line=source['line'], status=hyp['status'])
                           for unit, (source, hyp) in zip(units, rows)})
            request = output / 'requests' / (task_id + '.json')
            write(request, req)
            tasks.append(dict(id=task_id, dataset=name, condition_path=condition['path'],
                              request_sha256=digest(request), n=len(units)))
    counts = Counter(task['dataset'] for task in tasks)
    if counts != {'news_commentary': 24, 'guofeng': 24, 'naev': 90, 'naev_ablation_300': 150}:
        raise ValueError(f'Unexpected evaluation plan: {counts}')
    write(output / 'plan.json', dict(ready_at=now(), version='loong-domain-judge-v1',
        model=MODEL, endpoint=ENDPOINT, input_root=str(root.resolve()), input_files=files,
        source_note=source_note, template_sha256=copied_prompts, response_parameters='gateway defaults',
        request_counts=dict(counts), tasks=tasks, conditions=index,
        aggregation='unweighted five core scores per batch; equal-weight batches per condition'))
    print(f'Prepared {len(tasks)} requests: {dict(counts)}', flush=True)


def prompt_for(req, output):
    template = Template((output / 'prompts' / (req['template'] + '.txt')).read_text(encoding='utf-8'))
    return template.substitute(units_json=json.dumps(req['units'], ensure_ascii=False))


def validate_reply(text, req):
    text = text.strip()
    # Some gateways wrap valid JSON with a single fence.
    if text.startswith('```') and text.endswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    value = json.loads(text)
    fields = CORE + GAME if req['dataset'].startswith('naev') else CORE
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError('Wrong dimensions')
    ids = req['mapping']
    for field, detail in value.items():
        if not isinstance(detail, dict) or set(detail) != {'score', 'rationale', 'evidence_ids'}:
            raise ValueError('Wrong fields for ' + field)
        score = detail['score']
        if not (type(score) is int and 0 <= score <= 100 or field in GAME and score is None):
            raise ValueError('Invalid score for ' + field)
        if not isinstance(detail['rationale'], str) or not detail['rationale'].strip():
            raise ValueError('Missing rationale for ' + field)
        evidence = detail['evidence_ids']
        if not isinstance(evidence, list) or len(evidence) > 3 or any(
                not isinstance(i, str) or i not in ids for i in evidence):
            raise ValueError('Invalid evidence IDs for ' + field)
    return value


def completed(output, task):
    return (output / 'evaluations' / (task['id'] + '.json')).is_file()


def evaluate(client, task, output, stop, gate):
    request_path = output / 'requests' / (task['id'] + '.json')
    if digest(request_path) != task['request_sha256']:
        raise ValueError('Frozen request has changed: ' + task['id'])
    req = read(request_path)
    prompt = prompt_for(req, output)
    folder = output / 'attempts' / task['id']
    folder.mkdir(parents=True, exist_ok=True)
    tries = len(list(folder.glob('*.json')))
    for offset in range(4):
        gate.wait()
        if stop.is_set():
            return 'queued'
        entry = dict(task_id=task['id'], requested_model=MODEL, started_at=now(),
                     prompt_sha256=sha256(prompt.encode()).hexdigest(), status='inflight')
        file = folder / f'{tries + offset + 1:03d}.json'
        write(file, entry)
        start = time.monotonic()
        try:
            response = client.responses.create(model=MODEL, input=prompt)
            entry.update(response=response.model_dump(mode='json'), output_text=response.output_text)
            dimensions = validate_reply(response.output_text, req)
        except Exception as error:
            status_code = getattr(error, 'status_code', None)
            malformed = isinstance(error, (ValueError, KeyError, IndexError)) and 'response' in entry
            entry.update(status='invalid_reply' if malformed else 'request_failed', completed_at=now(),
                         elapsed_seconds=time.monotonic() - start, error_type=type(error).__name__,
                         http_status=status_code)
            if malformed:
                entry['validation_error'] = str(error)
            elif status_code:
                body = getattr(error, 'body', None)
                if isinstance(body, dict):
                    body = body.get('error', body)
                    if isinstance(body, dict):
                        entry['provider_error_code'] = body.get('code')
            if status_code == 429:
                response_headers = error.response.headers
                delay = response_headers.get('Retry-After', '')
                cooldown = max(12 * (offset + 1), float(delay) if delay.isdecimal() else 0)
                entry['cooldown_seconds'] = cooldown
                gate.pause(cooldown)
            write(file, entry)
            if status_code in (401, 402, 403):
                stop.set()
                return f'HTTP {status_code}'
            if (offset == 0 and malformed) or (offset < 3 and status_code == 429):
                continue
            return entry['status']
        entry.update(status='complete', completed_at=now(), elapsed_seconds=time.monotonic() - start)
        write(file, entry)
        write(output / 'evaluations' / (task['id'] + '.json'), dict(
            task_id=task['id'], attempt=str(file.relative_to(output)), dimensions=dimensions,
            score=mean(dimensions[k]['score'] for k in CORE), response_id=response.id,
            response_model=response.model))
        return 'complete'
    return 'failed'


class RequestGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.next_start = 0.0

    def wait(self):
        while True:
            with self.lock:
                current = time.monotonic()
                delay = self.next_start - current
                if delay <= 0:
                    self.next_start = current + 0.3
                    return
            time.sleep(delay)

    def pause(self, seconds):
        with self.lock:
            self.next_start = max(self.next_start, time.monotonic() + seconds)


def run(output, workers, pilot, retry_failed):
    from openai import OpenAI
    key = os.environ.get('TOKENDANCE_API_KEY')
    if not key:
        raise ValueError('Set TOKENDANCE_API_KEY in the environment')
    plan = read(output / 'plan.json')
    for name, checksum in plan['template_sha256'].items():
        if digest(output / 'prompts' / (name + '.txt')) != checksum:
            raise ValueError('Frozen template changed: ' + name)
    options = plan['tasks']
    if pilot:
        options = [next(t for t in options if t['dataset'] == name) for name in ('news_commentary', 'guofeng', 'naev')]
    else:
        options = list(options)
        random.Random(20261117).shuffle(options)
    with (output / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        gate = RequestGate()
        pending = [task for task in options if not completed(output, task) and
                   (retry_failed or not any((output / 'attempts' / task['id']).glob('*.json')))]
        with OpenAI(api_key=key, base_url=plan['endpoint'], max_retries=0, timeout=225.0) as client:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(evaluate, client, task, output, stop, gate): task for task in pending}
                for future in as_completed(futures):
                    task = futures[future]
                    print(now(), task['id'], task['dataset'], future.result(), flush=True)
        status(output)
        if any(not completed(output, task) for task in options):
            raise SystemExit(1)


def status(output):
    plan = read(output / 'plan.json')
    counts = Counter()
    for task in plan['tasks']:
        path = output / 'attempts' / task['id']
        attempts = sorted(path.glob('*.json'))
        category = 'complete' if completed(output, task) else (
            read(attempts[-1])['status'] if attempts else 'queued')
        counts[task['dataset'], category] += 1
    for name, total in plan['request_counts'].items():
        print(name, f'{counts[name, "complete"]}/{total}',
              {key: v for (dataset, key), v in counts.items() if dataset == name}, flush=True)


def summarize(output):
    plan = read(output / 'plan.json')
    root = Path(plan['input_root'])
    for rel, checksum in plan['input_files'].items():
        if digest(root / rel) != checksum:
            raise ValueError('Scored input changed: ' + rel)
    task_groups = defaultdict(list)
    for task in plan['tasks']:
        task_groups[task['condition_path']].append(task)
    conditions = []
    for condition in plan['conditions']:
        path = condition['path']
        tasks = task_groups[path]
        evaluations = [read(output / 'evaluations' / (t['id'] + '.json')) for t in tasks if completed(output, t)]
        applicable = defaultdict(list)
        for evaluation in evaluations:
            for key, detail in evaluation['dimensions'].items():
                if detail['score'] is not None:
                    applicable[key].append(detail['score'])
        all_done = len(tasks) == len(evaluations)
        conditions.append(dict(condition, judge=dict(llm=mean(e['score'] for e in evaluations) if all_done else None,
            completed=len(evaluations), expected=len(tasks), dimensions={
                k: dict(mean=mean(vals), applicable=len(vals)) for k, vals in applicable.items()}),
            existing_metrics=read(root / 'results' / path / 'scores.json')))
    usage = Counter()
    attempt_status = Counter()
    response_models = Counter()
    for file in (output / 'attempts').glob('*/*.json'):
        attempt = read(file)
        attempt_status[attempt['status']] += 1
        response = attempt.get('response', {})
        if response.get('model'):
            response_models[response['model']] += 1
        for field in ('input_tokens', 'output_tokens', 'total_tokens'):
            amount = (response.get('usage') or {}).get(field)
            if isinstance(amount, int):
                usage[field] += amount
    summary = dict(model=plan['model'], response_models=dict(response_models), attempt_status=dict(attempt_status),
                   usage=dict(usage), source_note=plan.get('source_note'), conditions=conditions)
    write(output / 'summary.json', summary)
    (output / 'report.md').write_text(render(summary), encoding='utf-8')
    return summary


def render(summary):
    by_path = {c['path']: c for c in summary['conditions']}
    display = {'qwen2.5-7B-Instruct': 'Qwen2.5-7B', 'qwen3-8B': 'Qwen3-8B', 'qwen3-14B': 'Qwen3-14B',
               'sentence': 'Sentence', 'doc2doc': 'Doc2Doc', 'delta': 'DelTA', 'framework': '完整框架',
               'baseline': 'Baseline', 'memory': '+ Memory', 'memory_graph': '+ Memory + Graph',
               'memory_graph_base_selector': '+ 未训练 selector', 'memory_graph_sft_dpo_selector': '+ SFT/DPO selector'}

    def f(value):
        return '—' if value is None else f'{value:.2f}'

    def metric(c, game=False):
        scores = c['existing_metrics']
        base = [100 * scores['scomet']['scomet']]
        if game:
            base += [scores['bleu']['bleu4'], 100 * scores['ltcr_common']['ltcr_1'],
                     100 * scores['ltcr_common']['ltcr_1f']]
        else:
            base += [100 * scores['dcomet']['dcomet']]
        return [f(v) for v in (*base, c['judge']['llm'])]

    lines = ['# LLM 评审结果', '', f'Judge 为 `{summary["model"]}`。']
    if source_note := summary.get('source_note'):
        lines.extend(['', f'评审使用{source_note}。'])
    lines += ['', '## 通用领域', '',
             '| 模型 | 方法 | News sCMT | News dCMT | News LLM | GuoFeng sCMT | GuoFeng dCMT | GuoFeng LLM |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for model in ('qwen2.5-7B-Instruct', 'qwen3-8B', 'qwen3-14B'):
        for j, method in enumerate(('sentence', 'doc2doc', 'delta', 'framework')):
            news = by_path[f'news_commentary/{model}/{method}/en-zh']
            gf = by_path[f'guofeng/{model}/{method}/zh-en']
            lines.append('| ' + ' | '.join([display[model] if j == 0 else '', display[method], *metric(news), *metric(gf)]) + ' |')
    for name, title in (('naev', 'Naev 100 条'), ('naev_ablation_300', 'Naev 300 条消融')):
        lines += ['', '## ' + title, '', '| 模型 | 方法 | sCMT | BLEU-4 | LTCR-1 | LTCR-1f | LLM |',
                  '|---|---|---:|---:|---:|---:|---:|']
        for path, c in by_path.items():
            dataset, model, method, _ = path.split('/')
            if dataset == name:
                lines.append('| ' + ' | '.join([display[model], display[method], *metric(c, game=True)]) + ' |')
    finished = sum(c['judge']['completed'] for c in summary['conditions'])
    expected = sum(c['judge']['expected'] for c in summary['conditions'])
    lines += ['', f'完成 {finished}/{expected} 个评审单元。缺少完整评审的条件用 — 表示。', '',
              'GuoFeng 为 selector-training seen。Naev 附加诊断保存在 summary.json，单独统计适用批次。',
              'LLM 列为核心五项的批次等权均分。']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=('prepare', 'pilot', 'run', 'status', 'summarize'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--root', type=Path)
    p.add_argument('--prompts', type=Path, default=Path('prompts/judge'))
    p.add_argument('--workers', type=int, default=8)
    p.add_argument('--retry-failed', action='store_true')
    args = p.parse_args()
    if args.stage == 'prepare':
        if args.root is None:
            p.error('--root is required for prepare')
        prepare(args.root, args.output, args.prompts)
    elif args.stage in ('pilot', 'run'):
        run(args.output, args.workers, args.stage == 'pilot', args.retry_failed)
    elif args.stage == 'status':
        status(args.output)
    else:
        summarize(args.output)
