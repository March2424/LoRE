"""Reuse the pinned DocMTAgent memory classes without importing its GPU CLI.

The four class AST nodes are executed unchanged from the upstream checkout.
Only their injected LLM transport and eval binding are replaced. No upstream
top-level imports, CLI, model loading or first-line translation truncation run.
"""
import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import random
from typing import List, Tuple


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def load_upstream(root, invoke, parse):
    path = Path(root) / 'infer/infer_qwen.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    names = {'RetrieveAgent', 'Summary', 'History', 'Context'}
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    if {n.name for n in nodes} != names:
        raise ValueError('Pinned DocMTAgent memory classes are missing')
    namespace = dict(List=List, Tuple=Tuple, deepcopy=deepcopy, random=random,
                     invoke_chat=invoke, eval=parse,
                     lang_dict={'en': 'English', 'zh': 'Chinese'}, SRC_SEP=' ', TGT_SEP='')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class DeltaMemory:
    def __init__(self, config, src_lang, tgt_lang, domain, call):
        self.config, self.call, self.domain = config, call, domain
        self.stage = ''
        self.diagnostics = []
        self.u = load_upstream(config['upstream'], self.invoke, self.parse_ids)
        self.u.update(SRC_SEP='' if src_lang == 'zh' else ' ',
                      TGT_SEP='' if tgt_lang == 'zh' else ' ')
        self.src_lang, self.tgt_lang = src_lang, tgt_lang
        root = Path(config['upstream']) / 'prompts'
        pair = root / (src_lang + '-' + tgt_lang)
        prompt = lambda name: (pair / (name + '_prompt.txt')).read_text(encoding='utf-8')
        self.summary = self.u['Summary'](*(prompt(n) for n in
            ('src_summary', 'tgt_summary', 'src_merge', 'tgt_merge')))
        self.history = self.u['History'](prompt('history'), src_lang, tgt_lang)
        self.context = self.u['Context'](config['context_window'])
        self.long = self.u['RetrieveAgent'](config['long_window'], 0.0, 10.0,
            (root / 'retrieve_prompt_qwen.txt').read_text(), config['context_window'])
        self.records = []

    def invoke(self, prompt):
        if self.domain == 'game' and self.stage == 'summary':
            prompt = ('These are independently processed game resources or summaries. '
                      'They need not be adjacent story events or compatible branches. '
                      'Summarize their content and style without inventing links between them.\n\n' + prompt)
        return self.call(self.stage, prompt)

    def parse_ids(self, value):
        # Official RetrieveAgent calls eval. Bind it to a bounded literal parser:
        # no execution of model output, no silent unlimited retrieval.
        try:
            ids = ast.literal_eval(value)
            if not isinstance(ids, list):
                raise ValueError('not a list')
            if any(type(i) is not int or not 1 <= i <= len(self.long.src_text_list) for i in ids):
                raise ValueError('out-of-pool ID')
            if len(ids) != len(set(ids)) or len(ids) != self.config['top_k']:
                raise ValueError('wrong retrieval count or duplicate ID')
            self.retrieved_indices = sorted(ids)
            return ids
        except (SyntaxError, ValueError, TypeError) as error:
            self.diagnostics.append('retrieval_empty_fallback: ' + str(error))
            return []

    def translate(self, row):
        self.diagnostics = []
        self.stage = 'retrieve'
        self.retrieved_indices = (list(range(1, len(self.long.src_text_list) + 1))
                                  if len(self.long.src_text_list) <= self.config['top_k'] else [])
        srcs, tgts = self.long.match(row['source'], self.config['top_k'])
        srcs, tgts = list(srcs), list(tgts)
        recent = self.records[-self.config['long_window']:]
        context_manifest = dict(
            current_source_id=row.get('source_id'), current_source_version=row.get('source_version', 1),
            long_source_ids=[recent[i-1].get('source_id') for i in self.retrieved_indices],
            short_source_ids=[r.get('source_id') for r in self.records[-self.config['context_window']:]],
            memory_version=len(self.records), unit_id=row.get('unit_id'),
            summary_through_record=(len(self.records) // self.config['summary_step']) * self.config['summary_step'],
            memory_origin='own previous generated translations; unreviewed experimental memory')
        src_context, tgt_context = self.context.get_context()
        lang = self.u['lang_dict']
        examples = '\n'.join(f'<{lang[self.src_lang]}> {s}\n<{lang[self.tgt_lang]}> {t}'
                             for s, t in zip(srcs, tgts))
        prompt = Path('prompts/delta/' + self.domain + '.txt').read_text().format(
            src_lang=lang[self.src_lang], tgt_lang=lang[self.tgt_lang], src=row['source'],
            rules=json.dumps(row.get('mandatory_rules', []), ensure_ascii=False),
            metadata=json.dumps(row.get('metadata', {}), ensure_ascii=False),
            src_summary=self.summary.src_summary or 'N/A', tgt_summary=self.summary.tgt_summary or 'N/A',
            hist_info=self.history.buildin_history(row['source'], True) or 'N/A',
            src_context=self.u['SRC_SEP'].join(src_context) or 'N/A',
            tgt_context=self.u['TGT_SEP'].join(tgt_context) or 'N/A', rel_inst=examples or 'N/A')
        text = self.call('translate', prompt)
        self.stage = 'summary'
        # Include the just-translated item: upstream main omitted it at boundaries.
        records = self.records + [dict(src=row['source'], hyp=text, source_id=row.get('source_id'))]
        if len(records) % self.config['summary_step'] == 0:
            self.summary.update_summary(records[-self.config['summary_step']:])
        self.long.insert(row['source'], text)
        self.stage = 'history'
        conflicts = self.history.extract_entity(row['source'], text)
        self.context.update(row['source'], text)
        self.records = records
        return text, dict(translation_prompt=prompt, retrieved_sources=list(srcs),
                          retrieved_targets=list(tgts), conflicts=conflicts,
                          context_manifest=context_manifest,
                          diagnostics=list(self.diagnostics))

    def snapshot(self):
        return dict(src_summary=self.summary.src_summary, tgt_summary=self.summary.tgt_summary,
                    entities=self.history.get_history_dict(),
                    short_src=self.context.src_context, short_tgt=self.context.tgt_context,
                    long_src=self.long.src_text_list, long_tgt=self.long.tgt_text_list,
                    records=self.records)

    def restore(self, snapshot):
        self.summary.set_summary(snapshot['src_summary'], snapshot['tgt_summary'])
        self.history.set_history_dict(snapshot['entities'])
        self.context.src_context, self.context.tgt_context = snapshot['short_src'], snapshot['short_tgt']
        self.long.src_text_list, self.long.tgt_text_list = snapshot['long_src'], snapshot['long_tgt']
        self.records = snapshot['records']


class LocalQwen:
    def __init__(self, path, config):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch, self.config, self.path = torch, config, path
        self.model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16,
            device_map={'': 0}, attn_implementation='sdpa', local_files_only=True).eval()
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)

    def __call__(self, task, prompt):
        tok = self.tokenizer
        formatted = tok.apply_chat_template([dict(role='user', content=prompt)], tokenize=False,
                                             add_generation_prompt=True, enable_thinking=False)
        inputs = tok(formatted, add_special_tokens=False, return_tensors='pt').to(self.model.device)
        n = inputs.input_ids.shape[1]
        limit = self.config['translation_max_new_tokens' if task == 'translate' else 'memory_max_new_tokens']
        if n + limit > min(self.config['max_context_tokens'], self.model.config.max_position_embeddings):
            raise ValueError('DelTA input exceeds window; no silent truncation')
        with self.torch.inference_mode():
            output = self.model.generate(**inputs, max_new_tokens=limit, do_sample=False,
                eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
        tokens = output[0, n:]
        text = tok.decode(tokens, skip_special_tokens=True).strip()
        return dict(text=text, model=self.path, input_tokens=n, output_tokens=len(tokens), cost=0.0,
                    finished=bool(len(tokens) and tokens[-1].item() == tok.eos_token_id))


class CachedCalls:
    """Persist each successful response before mutating memory; resume a partial job."""
    def __init__(self, folder, backend):
        self.folder, self.backend = Path(folder), backend
        self.index = 0
        self.replies = []

    def __call__(self, task, prompt):
        path = self.folder / f'{self.index:02d}_{task}.json'
        self.index += 1
        spec = dict(task=task, prompt=prompt)
        if path.exists():
            entry = read_json(path)
            if any(entry[k] != v for k, v in spec.items()):
                raise ValueError('Cached call input changed')
            if entry['status'] != 'complete':
                raise RuntimeError('Unresolved API call; review before explicitly retrying: ' + str(path))
        else:
            # Local generation can safely rerun; API uncertainty must remain visible.
            remote = getattr(self.backend, 'remote', False)
            if remote:
                write_json(path, dict(spec, status='inflight'))
            reply = self.backend(task, prompt)
            entry = dict(spec, status='complete', reply=reply)
            write_json(path, entry)
        self.replies.append(entry)
        if not entry['reply']['text'].strip() or not entry['reply'].get('finished', True):
            raise ValueError('Empty/unfinished response saved for review: ' + str(path))
        return entry['reply']['text']
