"""Run the sequential Naev held-out Agent evaluation and score it offline."""

import argparse
from collections import Counter
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import unicodedata

from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.preferences import CometLengthError, LocalSComet, lines
from localization_agent.selection.teacher import file_hash


SPLIT_ROOT = Path('outputs/cpu_preparation_2026-11-02_2000_context_v2')
DEPLOYMENT = Path('outputs/server_stage_inputs_2026-11-03/stage_inputs.json')
TRAINING = Path('outputs/selector_training')
QWEN = Path(os.environ.get('QWEN_8B', 'models/Qwen3-8B'))
SFT = Path(os.environ.get('SELECTOR_SFT', 'models/pi_sft_multidomain_e2_merged'))
DPO = Path(os.environ.get('SELECTOR_DPO', TRAINING / 'dpo_pro6000_20261107'))
COMET = Path(os.environ.get('COMET_CHECKPOINT', 'models/wmt22-comet-da/checkpoints/model.ckpt'))
COMET_SHA256 = 'e213091cde220f97b89f8bdfa750c458cfea741ad62affb455b59900210ff2af'
GRAPH_CONFIG = Path('configs/training/dpo_multidomain_graph.json')
PROMPT = Path('prompts/selection/context_selector.txt')
TRANSLATION_PROMPT = Path('prompts/memory/translate_game_small.txt')
TOKENIZER = Path('artifacts/retrieval_tokenizer/tokenizer.json')
TRANSLATION_POLICY = 'whole_source_prompt_v1'


def read(path):
    return json.loads(Path(path).read_text())


def normalize(source):
    return ' '.join(unicodedata.normalize('NFKC', source).casefold().split())


def prepare(folder):
    deploy = read(DEPLOYMENT)
    sft_data = Path(deploy['dataset'])
    dpo_data = TRAINING / 'dpo_final_20261107/selector_dpo_train.json'
    train = set()
    for path in (sft_data, dpo_data):
        for row in read(path):
            observation = json.loads(row['instruction'].rsplit('Observation: ', 1)[1])
            train.add(normalize(observation['source']))
    split_path = SPLIT_ROOT / 'splits.jsonl'
    source_path = SPLIT_ROOT / 'sources.jsonl'
    visible = {r['source_id']:r for r in lines(split_path)
               if r['game']=='naev' and r['split']=='external_test' and r['source_usable']}
    eligible = []
    for r in lines(source_path):
        if r['game']!='naev' or r['id'] not in visible:
            continue
        split = visible[r['id']]
        words = len(r['source_text'].split())
        if (split['score_candidate'] and not split['train_overlap'] and normalize(r['source_text']) not in train
                and (r['text_type']=='dialogue' and words>=25 or r['text_type']=='narrative' and words>=40)):
            eligible.append((r, words, split))
    eligible.sort(key=lambda x:(x[0]['text_type']!='dialogue',-x[1],x[0]['id']))
    unique = []
    seen = set()
    for entry in eligible:
        key = normalize(entry[0]['source_text'])
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    target = sorted(unique[:500],key=lambda x:(x[2]['position'],x[0]['id']))
    groups = {x[2]['group_id'] for x in target}
    warmup = sorted(unique[500:],key=lambda x:(x[2]['group_id'] not in groups,abs(x[1]-50),x[0]['id']))[:10]
    if len(target)!=500 or len(warmup)!=10:
        raise ValueError('Need 500 independent long targets and 10 separate warmups')

    graph = next(x for x in deploy['game_source_graphs'] if x['project_id']=='naev')
    graph_path = Path(graph['source_db'])
    if file_hash(graph_path)!=graph['sha256']:
        raise ValueError('Naev graph differs from frozen source snapshot')
    from localization_agent.memory import MemoryStore

    folder.mkdir(parents=True)
    source = sqlite3.connect(graph_path.resolve().as_uri()+'?mode=ro',uri=True)
    store = MemoryStore(folder/'naev.sqlite')
    try:
        if source.execute('SELECT count(*) FROM runs').fetchone()[0]:
            raise ValueError('Use the source-only graph for evaluation')
        for row, _, _ in warmup+target:
            payload = source.execute('SELECT payload FROM sources WHERE project=? AND id=?',('naev',row['id'])).fetchone()
            if not payload or json.loads(payload[0])['source']!=row['source_text']:
                raise ValueError('Source manifest and graph disagree for '+row['id'])
        source.backup(store.db)
        store.create_snapshot('teacher-knowledge','naev')
    finally:
        source.close()
        store.close()
    jobs = [dict(game='naev',run_id='naev_scomet:external_test',source_id=row['id'],
                 split='external_test',group_id=split['group_id'],phase=phase,status='pending',
                 trajectory_schema_version=2)
            for phase, batch in (('warmup',warmup),('target',target)) for row, _, split in batch]
    samples = [dict(source_id=row['id'],phase=phase,text_type=row['text_type'],words=words,
                    group_id=split['group_id'],position=split['position'])
               for phase,batch in (('warmup',warmup),('target',target)) for row,words,split in batch]
    manifest = dict(samples=samples, counts=dict(Counter(x[0]['text_type'] for x in target)),
                    word_min=min(x[1] for x in target),word_max=max(x[1] for x in target),
                    word_median=statistics.median(x[1] for x in target),group_count=len(groups),
                    graph=graph,split_sha256=file_hash(split_path),source_sha256=file_hash(source_path),
                    training_sha256={str(p):file_hash(p) for p in (sft_data,dpo_data)},
                    translator=str(QWEN),selector_base=str(SFT),selector_adapter=str(DPO),
                    selector_sha256=file_hash(DPO/'adapter_model.safetensors'),
                    translation_decoding=TRANSLATION_POLICY,
                    translation_prompt_sha256=file_hash(TRANSLATION_PROMPT),
                    runner_sha256=file_hash(__file__))
    config = dict(graph=read(GRAPH_CONFIG),tokenizer=str(TOKENIZER),
                  allowed_source_ids={'naev':sorted(visible)},
                  memory=dict(stm_size=3,ltm_top_k=2,summary_step=5,style_min_examples=2),
                  models={'translation':read('configs/flare.memory_trial.json')['model']},
                  request_retry=dict(max_attempts=4,initial_delay_seconds=2))
    write_json(folder/'manifest.json',manifest)
    write_json(folder/'config.json',config)
    write_json(folder/'state.json',dict(status='prepared',memory_policy='per_game_split_stage_v1',jobs=jobs))
    write_json(folder/'budget.json',dict(max_cost=3.0,max_calls=2000,max_tokens=6000000))
    print({k:manifest[k] for k in ('counts','word_min','word_max','word_median','group_count')},flush=True)


def load_qwen(path):
    import torch
    from transformers import AutoModelForCausalLM

    return AutoModelForCausalLM.from_pretrained(path,dtype=torch.bfloat16,device_map={'':0},
        attn_implementation='sdpa',local_files_only=True).eval()


def generate(model, tokenizer, prompt, *, selector, retry_seed=None, disable_thinking=False):
    import torch

    if selector and not disable_thinking:
        formatted='<|im_start|>user\n'+prompt+'<|im_end|>\n<|im_start|>assistant\n'
    else:
        formatted=tokenizer.apply_chat_template([{'role':'user','content':prompt}],tokenize=False,
                                                 add_generation_prompt=True,enable_thinking=False)
    ids=tokenizer(formatted,add_special_tokens=False,return_tensors='pt').to(model.device)
    count=ids.input_ids.shape[1]
    if selector and count+1024>8192:
        raise ValueError('Selector prompt exceeds 8192-token policy window')
    decoding={'do_sample':False}
    if retry_seed is not None:
        torch.manual_seed(retry_seed)
        decoding=dict(do_sample=True,temperature=0.45,top_p=0.85,repetition_penalty=1.08)
    with torch.inference_mode():
        output=model.generate(**ids,max_new_tokens=1024 if selector else 768,**decoding,
                              eos_token_id=tokenizer.eos_token_id,pad_token_id=tokenizer.pad_token_id)
    new=output[0,count:]
    return (tokenizer.decode(new,skip_special_tokens=True).strip(),count,new.numel(),
            new.numel()>0 and new[-1].item()==tokenizer.eos_token_id)


def translate_game_resource(source, prompt, generate_reply, placeholders, seed):
    """Ask for complete plain-text translation, with one retry for generation or format errors."""
    _, marker, context = prompt.partition(f'Source: {source}\nCurrent resource context: ')
    resource_context, separator, selected_context = context.partition('\nSelected context: ')
    if not marker or not separator:
        raise ValueError('Translation prompt has no matching source and context blocks')
    template = TRANSLATION_PROMPT.read_text()
    source_placeholders = placeholders(source)

    attempts, accepted = [], None
    for attempt in range(2):
        repair = ''
        if attempt:
            repair = 'Translate the complete source again and preserve its formatting.\n'
            if source_placeholders:
                repair += 'Preserve these placeholders exactly: '+json.dumps(source_placeholders,ensure_ascii=False)+'\n'
        actual = template.format(resource_context=resource_context,selected_context=selected_context.rstrip(),
                                 repair=repair,source=source)
        raw,input_tokens,output_tokens,finished = generate_reply(actual,retry_seed=seed if attempt else None)
        issue = None
        if not raw or not finished:
            issue = 'Local translator returned empty or unfinished text'
        elif Counter(source_placeholders)!=Counter(placeholders(raw)):
            issue = 'Placeholder/tag mismatch'
        attempts.append(dict(prompt=actual,raw=raw,input_tokens=input_tokens,output_tokens=output_tokens,
                             finished=finished,issue=issue))
        if accepted is None or not issue or (raw and (finished or not accepted[3])):
            accepted = (raw,input_tokens,output_tokens,finished,attempt)
        if not issue:
            break
    return accepted,attempts


def run(folder):
    from peft import PeftModel
    from transformers import AutoTokenizer
    from localization_agent.models.chat_completions import ChatCompletionsModel
    from localization_agent.models.protocols import ModelReply
    from localization_agent.selection.collector import Collector

    config = read(folder/'config.json')
    manifest = read(folder/'manifest.json')
    if (manifest.get('translation_decoding')!=TRANSLATION_POLICY or
            manifest.get('translation_prompt_sha256')!=file_hash(TRANSLATION_PROMPT)):
        raise ValueError('Prepare a new evaluation to use the corrected translator with clean memory')
    if not os.environ.get(config['models']['translation']['api_key_env']):
        raise RuntimeError('Missing DeepSeek credential')
    tokenizer = AutoTokenizer.from_pretrained(QWEN,local_files_only=True)

    class EvaluationCollector(Collector):
        def process_job(self, job, store, memory, retriever, session, call):
            self.game_session=session
            return super().process_job(job,store,memory,retriever,session,call)

        def collect_selection(self, job, retriever, pool, call):
            observation=retriever.selector_observation(pool)
            job['observation']=observation
            job['student_prompt']=PROMPT.read_text().format(observation=json.dumps(observation,ensure_ascii=False))
            try:
                raw, _, _, finished=generate(selector,tokenizer,job['student_prompt'],selector=True)
                job['selector_reply']=raw
                if not finished:
                    raise ValueError('Selector did not finish')
                action=json.loads(raw)
                ids=action['selected_ids']
                if not action['rationale'].strip() or not isinstance(ids,list):
                    raise ValueError('Invalid selector action')
                pack=retriever.select_context(pool,tuple(ids))
                if pack.status!='ready':
                    raise ValueError(pack.status)
                job['selector_status']='dpo'
            except (ValueError,KeyError,TypeError,AttributeError) as error:
                default=retriever.build_context(pool)
                selected={c.id for c in default.selected}
                soft=[c for c in pool.candidates if not c.mandatory]
                ids=[f'c{i:02d}' for i,c in enumerate(soft,1) if c.id in selected]
                action={'rationale':'Deterministic retrieval fallback','selected_ids':ids}
                job['selector_status']='retrieval_fallback'
                job['issues'].append('DPO selection: '+str(error))
            job.update(action=action,selection_checks_passed=True,
                       selected_contexts=[x for x in observation['candidates'] if x['id'] in ids])

        def call(self, game, job, task, prompt):
            if task!='translate_game':
                return super().call(game,job,task,prompt)
            if 'eval_reply' not in job:
                seed=int.from_bytes(job['source_id'].encode()[-4:],'big')
                (target,input_tokens,output_tokens,finished,accepted), attempts = translate_game_resource(
                    job['source'],prompt,lambda prompt,**kw:generate(translator,tokenizer,prompt,selector=False,**kw),
                    self.game_session.placeholders,seed)
                job.update(translation_attempts=attempts,translation_attempt=accepted)
                job['eval_reply']=dict(text=target,model='Qwen3-8B',input_tokens=input_tokens,
                                       output_tokens=output_tokens,cost=0.0)
                job['eval_finished']=finished
                self.checkpoints.save(job)
            reply=ModelReply(**job['eval_reply'])
            if not reply.text or not job['eval_finished']:
                raise ValueError(job['translation_attempts'][-1]['issue'] or 'Incomplete local translation')
            job['translation_prompt']=job['translation_attempts'][job['translation_attempt']]['prompt']
            return reply

    with (folder/'.run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        translator=load_qwen(QWEN)
        selector=PeftModel.from_pretrained(load_qwen(SFT),DPO,is_trainable=False).eval()
        print('Loaded Qwen3-8B Translator and DPO Selector',flush=True)
        final=EvaluationCollector(folder,config,{},ChatCompletionsModel,1,1).run()
        print('Agent stage:',final['status'],flush=True)
        if final['status'] not in {'complete','complete_with_warnings','complete_with_failures'}:
            raise SystemExit(2)


def score(folder, *, stopped_only=False):
    state=load_state(folder)
    jobs=[job for job in state['jobs'] if job['phase']=='target']
    if stopped_only:
        if state['status']!='stopped':
            raise ValueError('Finish the cooperative stop before partial scoring')
        jobs=[job for job in jobs if job['status'] in {'complete','failed'}]
    elif len(jobs)!=500 or any(job['status'] not in {'complete','failed'} for job in jobs):
        raise ValueError('Score only after all 500 target attempts finish')
    if not jobs:
        raise ValueError('No formal targets finished')
    # Only this offline process opens target references.
    wanted={job['source_id'] for job in jobs}
    refs={r['record_id']:r['reference_text'] for r in lines(SPLIT_ROOT/'offline/references.jsonl')
          if r['game']=='naev' and r['split']=='external_test' and r['record_id'] in wanted}
    sources={r['id']:r['source_text'] for r in lines(SPLIT_ROOT/'sources.jsonl')
             if r['game']=='naev' and r['split']=='external_test' and r['id'] in wanted}
    if refs.keys()!=wanted or sources.keys()!=wanted or any(not r.strip() for r in refs.values()):
        raise ValueError('Frozen target references do not match the evaluation IDs')
    scorer=LocalSComet(COMET,batch_size=8,gpus=1)
    if scorer.identity['sha256']!=COMET_SHA256:
        raise ValueError('Unexpected sCOMET checkpoint')
    instances, rows = [], []
    for job in jobs:
        key=job['source_id']
        mt=job.get('eval_reply',{}).get('text','')
        item=dict(src=sources[key],mt=mt,ref=refs[key])
        row=dict(source_id=key,agent_status=job['status'],empty_hypothesis=not mt,scomet=None)
        rows.append(row)
        try:
            scorer.validate_length(item)
        except CometLengthError as error:
            item['mt']=''
            scorer.validate_length(item)  # Still reject overlong source/reference input.
            row.update(empty_hypothesis=True,reason=str(error),oversize_hypothesis_as_empty=True)
        instances.append(item)
    scores=scorer.score(instances)
    if len(scores)!=len(rows) or not all(math.isfinite(value) for value in scores):
        raise ValueError('Unexpected sCOMET result')
    for row,value in zip(rows,scores):
        row['scomet']=float(value)
    result=dict(scomet=statistics.mean(scores), scored=len(scores),requested=500,partial=stopped_only,
        empty_hypotheses=sum(row['empty_hypothesis'] for row in rows),
        oversize_hypotheses_as_empty=sum(row.get('oversize_hypothesis_as_empty',False) for row in rows),
        translation_failed=sum(job['status']=='failed' for job in jobs),
        selector_status=dict(Counter(job.get('selector_status','not_attempted') for job in jobs)),
        target_memory_status=dict(Counter(job.get('memory_status','missing') for job in jobs)),
        warmup_status=dict(Counter(job['status'] for job in state['jobs'] if job['phase']=='warmup')),
        budget=read(folder/'budget.json'),scorer=scorer.identity,score_runner_sha256=file_hash(__file__))
    prefix='partial_' if stopped_only else ''
    write_json(folder/f'{prefix}scores.json',rows)
    write_json(folder/f'{prefix}result.json',result)
    print(json.dumps(result,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('prepare','run','score','score-stopped'))
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if args.stage=='score-stopped':
        score(args.output,stopped_only=True)
    else:
        {'prepare':prepare,'run':run,'score':score}[args.stage](args.output)


if __name__=='__main__':
    main()
