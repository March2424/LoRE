"""Offline checks of the actual upstream classes and our experiment adapter."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from delta_runtime import CachedCalls, DeltaMemory, read_json, write_json
from run_delta_eval import execute_rows


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, task, prompt):
        self.calls.append((task, prompt))
        text = {'translate': '叙述句。\n“台词。”', 'retrieve': '[1, 2]',
                'history': '"Mira" - "米拉"', 'summary': 'A summary.'}[task]
        return dict(text=text, model='mock', input_tokens=1, output_tokens=1, cost=0.0)


@unittest.skipUnless(Path('third_party/DocMTAgent/infer/infer_qwen.py').exists(), 'clone pinned upstream first')
class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.config = read_json('configs/delta_eval.json')
        self.config['summary_step'] = 2

    def test_upstream_components_multiline_summary_and_safe_retrieval(self):
        fake = Fake()
        memory = DeltaMemory(self.config, 'en', 'zh', 'game', lambda t, p: fake(t, p)['text'])
        for i in range(5):
            text, trace = memory.translate(dict(source=f'Mira event {i}'))
            self.assertEqual(text, '叙述句。\n“台词。”')
        self.assertEqual(len(memory.context.src_context), 3)
        self.assertEqual(memory.history.entity_dict['Mira'], '米拉')
        summary = [p for t, p in fake.calls if t == 'summary']
        self.assertIn('Mira event 1', summary[0])  # boundary includes current item
        self.assertEqual(len(summary), 6)  # first generation + next generation/merge
        self.assertEqual(memory.parse_ids("__import__('os').getcwd()"), [])
        self.assertEqual(memory.parse_ids('[1, 1]'), [])
        self.assertEqual(memory.parse_ids('[1, 2, 3]'), [])

    def test_checkpoint_resume_and_document_reset(self):
        fake = Fake()
        rows = [dict(source_id=str(i), source='Mira event ' + str(i), phase='target',
                     dataset='news', unit_id='a' if i < 2 else 'b', source_lang='en', target_lang='zh')
                for i in range(3)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            write_json(path / 'state.json', dict(status='prepared', jobs=[
                dict(source_id=r['source_id'], phase='target', status='pending') for r in rows]))
            self.assertTrue(execute_rows(path, rows, self.config, fake))
            calls = len(fake.calls)
            self.assertTrue(execute_rows(path, rows, self.config, fake))
            self.assertEqual(len(fake.calls), calls)
            job = read_json(path / 'jobs/000002.json')
            self.assertEqual(len(job['memory_after']['records']), 1)
            self.assertEqual(job['retrieved_sources'], [])
            self.assertNotIn('A summary.', job['translation_prompt'])

    def test_partial_job_reuses_translation_and_blocks_unknown_api_call(self):
        fake = Fake()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            first = CachedCalls(path, fake)
            first('translate', 'Complete source')
            resumed = CachedCalls(path, fake)
            self.assertEqual(resumed('translate', 'Complete source'), '叙述句。\n“台词。”')
            self.assertEqual(len(fake.calls), 1)
            write_json(path / '01_history.json', dict(status='inflight', task='history', prompt='p'))
            with self.assertRaisesRegex(RuntimeError, 'Unresolved API'):
                resumed('history', 'p')


if __name__ == '__main__':
    unittest.main()
