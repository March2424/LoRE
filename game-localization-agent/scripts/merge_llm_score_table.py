"""Write tables that combine complete Judge reviews with the original evaluation scores."""
import argparse
from collections import defaultdict
import math
from pathlib import Path
from statistics import mean

from run_llm_judge import CORE, digest, read, write


DISPLAY = {
    'qwen2.5-7B-Instruct': 'Qwen2.5-7B', 'qwen3-8B': 'Qwen3-8B', 'qwen3-14B': 'Qwen3-14B',
    'sentence': 'Sentence', 'doc2doc': 'Doc2Doc', 'delta': 'DelTA', 'framework': '完整框架',
    'baseline': 'Baseline', 'memory': '+ Memory', 'memory_graph': '+ Memory + Graph',
    'memory_graph_base_selector': '+ Memory + Graph + 未训练 selector',
    'memory_graph_sft_dpo_selector': '+ Memory + Graph + SFT/DPO selector',
}
GENERAL = ('sCMT', 'dCMT', 'LLM', 'Avg')
GAME = ('sCMT', 'BLEU-4', 'LTCR-1', 'LTCR-1f', 'LLM', 'Avg')


def collect(judge, results):
    plan = read(judge / 'plan.json')
    summary = read(judge / 'summary.json')
    if results.resolve() != (Path(plan['input_root']) / 'results').resolve():
        raise ValueError('Judge snapshot points to a different results folder')
    for rel, checksum in plan['input_files'].items():
        if digest(results.parent / rel) != checksum:
            raise ValueError('Frozen Judge input has changed: ' + rel)
    by_path = {row['path']: row for row in summary['conditions']}
    task_groups = defaultdict(list)
    for task in plan['tasks']:
        path = judge / 'evaluations' / (task['id'] + '.json')
        review = read(path)
        score = mean(review['dimensions'][key]['score'] for key in CORE)
        if review['task_id'] != task['id'] or not math.isclose(score, review['score'], abs_tol=1e-9):
            raise ValueError('Judge unit score differs: ' + task['id'])
        task_groups[task['condition_path']].append(score)

    rows = []
    for condition in read(results / 'manifest.json')['conditions']:
        path = condition['path']
        scores = read(results / path / 'scores.json')
        item = by_path[path]
        llm = item['judge']
        task_scores = task_groups[path]
        if (scores != item['existing_metrics'] or len(task_scores) != llm['expected']
                or len(task_scores) != llm['completed'] or llm['llm'] is None
                or not math.isclose(mean(task_scores), llm['llm'], abs_tol=1e-9)):
            raise ValueError('Judge summary disagrees with the complete evaluation: ' + path)
        dataset, model, method, direction = path.split('/')
        metrics = {'sCMT': 100 * scores['scomet']['scomet']}
        if condition['experiment'] == 'general':
            metrics['dCMT'] = 100 * scores['dcomet']['dcomet']
        else:
            metrics.update({'BLEU-4': scores['bleu']['bleu4'],
                            'LTCR-1': 100 * scores['ltcr_common']['ltcr_1'],
                            'LTCR-1f': 100 * scores['ltcr_common']['ltcr_1f']})
        metrics['LLM'] = llm['llm']
        metrics['Avg'] = mean(metrics.values())
        rows.append(dict(path=path, dataset=dataset, model=model, method=method,
                         direction=direction, n=condition['n'], metrics=metrics, judge=llm))
    if len(rows) != len(by_path) or len(rows) != len(task_groups) or len(rows) != 38 or len(plan['tasks']) != 288:
        raise ValueError('Expected 38 complete conditions and 288 Judge units')
    return dict(judge_model=summary['model'], response_models=summary['response_models'],
                judge_completed=len(plan['tasks']), source_note=summary.get('source_note'),
                judge_summary_path=str(judge.resolve() / 'summary.json'),
                judge_summary_sha256=digest(judge / 'summary.json'),
                average={'general': list(GENERAL[:-1]), 'naev': list(GAME[:-1]),
                         'method': 'equal-weight arithmetic mean of unrounded scores on the 0–100 display scale'},
                rows=rows)


def render(table):
    rows = table['rows']
    index = {(r['dataset'], r['model'], r['method']): r for r in rows}

    def values(row, fields, peers):
        return [f'**{row["metrics"][k]:.2f}**' if row['metrics'][k] == max(p['metrics'][k] for p in peers)
                else f'{row["metrics"][k]:.2f}' for k in fields]

    def line(items):
        return '| ' + ' | '.join(items) + ' |'

    lines = ['# 译文评分表', '',
             f'LLM Judge 使用 `{table["judge_model"]}`，已完成 {table["judge_completed"]} 个评审单元。'
             '粗体表示组内最高值。Avg 先统一到百分制，再对该数据集显示的指标等权平均，最后保留两位小数。',
             '', '## 通用领域', '',
             line(['模型', '方法', *(f'{ds} {field}' for ds in ('News', 'GuoFeng') for field in GENERAL)]),
             line(['---', '---', *(['---:'] * 8)])]
    models = ('qwen2.5-7B-Instruct', 'qwen3-8B', 'qwen3-14B')
    for model in models:
        for i, method in enumerate(('sentence', 'doc2doc', 'delta', 'framework')):
            cells = [DISPLAY[model] if i == 0 else '', DISPLAY[method]]
            for ds in ('news_commentary', 'guofeng'):
                peers = [r for r in rows if r['dataset'] == ds and r['model'] == model]
                cells.extend(values(index[ds, model, method], GENERAL, peers))
            lines.append(line(cells))

    lines += ['', '## Naev 100 条', '', line(['模型', '方法', *GAME]), line(['---', '---', *(['---:'] * 6)])]
    for model in models:
        peers = [r for r in rows if r['dataset'] == 'naev' and r['model'] == model]
        for i, method in enumerate(('sentence', 'delta', 'framework')):
            lines.append(line([DISPLAY[model] if i == 0 else '', DISPLAY[method],
                               *values(index['naev', model, method], GAME, peers)]))

    lines += ['', '## Naev 300 条消融', '', line(['方法', *GAME]), line(['---', *(['---:'] * 6)])]
    peers = [r for r in rows if r['dataset'] == 'naev_ablation_300']
    for method in ('baseline', 'memory', 'memory_graph', 'memory_graph_base_selector', 'memory_graph_sft_dpo_selector'):
        lines.append(line([DISPLAY[method], *values(index['naev_ablation_300', 'qwen3-14B', method], GAME, peers)]))
    lines += ['', 'GuoFeng 为选择器训练内数据。']
    if source_note := table.get('source_note'):
        lines.append(f'评分使用{source_note}。')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--judge', required=True, type=Path)
    parser.add_argument('--results', required=True, type=Path)
    args = parser.parse_args()
    table = collect(args.judge, args.results)
    write(args.results / 'score_table.json', table)
    report = args.results / 'score_table.md'
    temporary = report.with_suffix('.tmp')
    temporary.write_text(render(table), encoding='utf-8')
    temporary.replace(report)
    print(report.read_text(encoding='utf-8'), end='')
