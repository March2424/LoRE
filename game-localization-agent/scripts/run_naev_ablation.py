"""Run four sequential, independently remembered Naev ablation conditions."""
import argparse
from collections import Counter
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import random
import sqlite3

from localization_agent.memory import MemoryStore
from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.teacher import file_hash
from run_naev_scomet import TRANSLATION_POLICY


ROOT = Path('outputs/naev_scomet_500_20261107_v2')
GRAPH = Path('outputs/naev_full_2026-11-01/graph.sqlite')
TRANSLATOR = Path(os.environ.get('QWEN_14B', 'models/Qwen3-14B'))
CONDITIONS = ('baseline','memory','graph','selector')


def prepare(folder):
    previous=json.loads((ROOT/'manifest.json').read_text())
    warm=[x for x in previous['samples'] if x['phase']=='warmup']
    targets=[x for x in previous['samples'] if x['phase']=='target']
    if len(warm)!=10 or len(targets)!=500:
        raise ValueError('Expected the frozen 10/500 Naev sample')
    sample=random.Random(20261109).sample(targets,300)
    sample.sort(key=lambda x:(x['position'],x['source_id']))
    config=json.loads((ROOT/'config.json').read_text())
    manifest={'samples':warm+sample,'sample_seed':20261109,'source_manifest_sha256':file_hash(ROOT/'manifest.json'),
              'graph_sha256':file_hash(GRAPH),'translator':str(TRANSLATOR),'conditions':CONDITIONS,
              'selector':'SFT E2 merged + DPO LoRA','memory_model':config['models']['translation']['model'],
              'translator_prompt_sha256':file_hash('prompts/memory/translate_game_small.txt'),
              'method':'baseline without warmup or runtime memory; independent sequential memories for other conditions',
              'counts':dict(Counter(x['text_type'] for x in sample))}
    folder.mkdir(parents=True)
    write_json(folder/'manifest.json',manifest)
    for condition in CONDITIONS:
        out=folder/condition
        out.mkdir()
        original=sqlite3.connect(GRAPH.resolve().as_uri()+'?mode=ro',uri=True)
        store=MemoryStore(out/'naev.sqlite')
        try:
            if original.execute('SELECT COUNT(*) FROM runs').fetchone()[0]:
                raise ValueError('Expected source-only graph')
            original.backup(store.db)
            store.create_snapshot('teacher-knowledge','naev')
        finally:
            original.close()
            store.close()
        jobs=[{'game':'naev','run_id':'ablation:'+condition,'source_id':x['source_id'],
               'split':'external_test','group_id':x['group_id'],'phase':x['phase'],'status':'pending',
               'trajectory_schema_version':2} for x in (sample if condition=='baseline' else warm+sample)]
        write_json(out/'manifest.json',{'translation_decoding':TRANSLATION_POLICY,
                   'translation_prompt_sha256':manifest['translator_prompt_sha256']})
        write_json(out/'config.json',config)
        write_json(out/'state.json',{'status':'prepared','memory_policy':'per_game_split_stage_v1','jobs':jobs})
        write_json(out/'budget.json',{'max_cost':3.0,'max_calls':1800,'max_tokens':6000000})
    print('Prepared identical 300 targets and 10 warmups for',CONDITIONS,manifest['counts'],flush=True)


def run(folder, condition, *, selector_variant='dpo'):
    import os
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from localization_agent.models.chat_completions import ChatCompletionsModel
    from localization_agent.models.protocols import ModelReply
    from localization_agent.retrieval.service import GraphRetriever, candidate_from_memory
    from localization_agent.schemas.graph import CandidatePool
    from localization_agent.schemas.memory import is_mandatory
    from localization_agent.selection.collector import Collector
    from run_naev_scomet import DPO, PROMPT, QWEN, SFT, TOKENIZER, TRANSLATION_PROMPT, generate, translate_game_resource

    out=folder/condition
    config=json.loads((out/'config.json').read_text())
    manifest=json.loads((folder/'manifest.json').read_text())
    if file_hash(TRANSLATION_PROMPT)!=manifest['translator_prompt_sha256']:
        raise ValueError('Frozen translator prompt changed')
    if condition != 'baseline' and not os.environ.get(config['models']['translation']['api_key_env']):
        raise ValueError('Missing memory API credential')
    tokenizer=AutoTokenizer.from_pretrained(TRANSLATOR,local_files_only=True)
    translator=AutoModelForCausalLM.from_pretrained(TRANSLATOR,dtype=torch.bfloat16,device_map={'':0},
        attn_implementation='sdpa',local_files_only=True).eval()
    selector=None
    if condition=='selector':
        if selector_variant=='base':
            selector=AutoModelForCausalLM.from_pretrained(QWEN,dtype=torch.bfloat16,
                device_map={'':'cpu'},local_files_only=True).eval()
            selector_tokenizer=AutoTokenizer.from_pretrained(QWEN,local_files_only=True)
        elif selector_variant=='dpo':
            selector=PeftModel.from_pretrained(AutoModelForCausalLM.from_pretrained(SFT,dtype=torch.bfloat16,
                device_map={'':'cpu'},local_files_only=True),DPO,is_trainable=False).eval()
            selector_tokenizer=AutoTokenizer.from_pretrained(SFT,local_files_only=True)
        else:
            raise ValueError('Unknown selector variant: '+selector_variant)

    original=GraphRetriever.retrieve
    def retrieve(self,occurrence,view,snapshot_id):
        if condition in ('graph','selector'):
            return original(self,occurrence,view,snapshot_id)
        if condition=='baseline':
            return CandidatePool(occurrence.occurrence_id,occurrence.source_version,view,snapshot_id,())
        bundle=self.memory.read(occurrence,view,related_ids=set())
        records=[r for group in (bundle.terms,bundle.styles,bundle.stm,bundle.ltm,bundle.summaries) for r in group]
        if self.allowed_source_ids is not None:
            records=[r for r in records if all(e.occurrence_id in self.allowed_source_ids for e in r.evidence)]
        hard=[candidate_from_memory(r,mandatory=True) for r in records if is_mandatory(r)]
        soft=[candidate_from_memory(r) for r in records if not is_mandatory(r)]
        query=self.query_tokens(occurrence.source)
        soft.sort(key=lambda c:(-len(query & self.query_tokens(c.text)),c.id))
        return CandidatePool(occurrence.occurrence_id,occurrence.source_version,view,snapshot_id,
                             tuple(hard+soft[:self.config.max_candidates]),bundle.conflicts)
    GraphRetriever.retrieve=retrieve

    class Experiment(Collector):
        def process_job(self,job,store,memory,retriever,session,call):
            self.session=session
            if condition=='baseline':
                from localization_agent.schemas.memory import Occurrence
                from localization_agent.selection.teacher import translation_issues
                payload=store.db.execute('SELECT payload FROM sources WHERE project=? AND id=? ORDER BY version DESC LIMIT 1',
                                         (job['game'],job['source_id'])).fetchone()
                source=Occurrence(**json.loads(payload[0]))
                job.update(source=source.source,status='working',issues=[],view=asdict(store.view(job['run_id'])))
                self.checkpoints.save(job)
                try:
                    pool=retriever.retrieve(source,store.view(job['run_id']),'graph')
                    pack=retriever.build_context(pool,())
                    if pack.status!='ready':raise ValueError('Context not ready: '+pack.status)
                    job['translation_context']=asdict(pack)
                    prompt=(memory.config.prompt_dir/'translate_game.txt').read_text().format(
                        source=source.source,source_lang=source.source_lang,target_lang=source.target_lang,
                        contexts=pack.model_context,source_context=pack.manifest['current_source_context'])
                    job['translation_prompt']=prompt
                    reply=call('translate_game',prompt)
                    issues=translation_issues(source,reply.text,pack,session)
                    job.update(status='complete' if not issues else 'failed',issues=issues,
                               memory_status='disabled',selection_checks_passed=True,selector_status='fixed')
                except Exception as error:
                    job.update(status='failed',memory_status='disabled')
                    job['issues'].append(type(error).__name__+': '+str(error))
                self.checkpoints.save(job)
                return None
            return super().process_job(job,store,memory,retriever,session,call)

        def collect_selection(self,job,retriever,pool,call):
            obs=retriever.selector_observation(pool)
            job['observation']=obs
            if condition=='selector':
                prompt=PROMPT.read_text().format(observation=json.dumps(obs,ensure_ascii=False))
                job['student_prompt']=prompt
                try:
                    translator.to('cpu')
                    torch.cuda.empty_cache()
                    selector.to('cuda')
                    raw,_,_,finished=generate(selector,selector_tokenizer,prompt,selector=True,
                                               disable_thinking=selector_variant=='base')
                    job['selector_reply']=raw
                    if not finished: raise ValueError('unfinished selector output')
                    action=json.loads(raw)
                    ids=action['selected_ids']
                    if not action['rationale'].strip() or not isinstance(ids,list):raise ValueError('invalid action')
                    if retriever.select_context(pool,tuple(ids)).status!='ready':raise ValueError('context not ready')
                    job['selector_status']=selector_variant
                except (ValueError,KeyError,TypeError,AttributeError) as error:
                    job['issues'].append('selector fallback: '+str(error))
                    job['selector_status']='fallback'
                finally:
                    selector.to('cpu')
                    torch.cuda.empty_cache()
                    translator.to('cuda')
            else:
                job['selector_status']='fixed'
            if job['selector_status'] not in ('dpo','base'):
                selected={c.id for c in retriever.build_context(pool).selected}
                ids=[f'c{i:02d}' for i,c in enumerate((c for c in pool.candidates if not c.mandatory),1) if c.id in selected]
                action={'rationale':'fixed ranking','selected_ids':ids}
            job.update(action=action,selection_checks_passed=True,
                       selected_contexts=[x for x in obs['candidates'] if x['id'] in ids])

        def call(self,game,job,task,prompt):
            if task!='translate_game':
                return super().call(game,job,task,prompt)
            if 'eval_reply' not in job:
                seed=int.from_bytes(job['source_id'].encode()[-4:],'big')
                (target,n,m,finished,accepted),attempts=translate_game_resource(job['source'],prompt,
                    lambda p,**kw:generate(translator,tokenizer,p,selector=False,**kw),self.session.placeholders,seed)
                job.update(eval_reply={'text':target,'model':str(TRANSLATOR),'input_tokens':n,'output_tokens':m,'cost':0.0},
                           eval_finished=finished,translation_attempts=attempts,translation_attempt=accepted)
                self.checkpoints.save(job)
            if not job['eval_reply']['text'] or not job['eval_finished']:
                raise ValueError('Local translation unfinished')
            job['translation_prompt']=job['translation_attempts'][job['translation_attempt']]['prompt']
            return ModelReply(**job['eval_reply'])

    with (out/'.run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        result=Experiment(out,config,{},ChatCompletionsModel,1,1).run()
        print(condition,result['status'],flush=True)
        if result['status'] not in ('complete','complete_with_warnings','complete_with_failures'):
            raise SystemExit(2)


def score(folder):
    from statistics import mean
    from localization_agent.selection.preferences import LocalSComet,CometLengthError,lines
    from run_naev_scomet import COMET,COMET_SHA256,SPLIT_ROOT
    manifest=json.loads((folder/'manifest.json').read_text())
    wanted={x['source_id'] for x in manifest['samples'] if x['phase']=='target'}
    refs={r['record_id']:r['reference_text'] for r in lines(SPLIT_ROOT/'offline/references.jsonl')
          if r['game']=='naev' and r['split']=='external_test' and r['record_id'] in wanted}
    sources={r['id']:r['source_text'] for r in lines(SPLIT_ROOT/'sources.jsonl') if r['game']=='naev' and r['id'] in wanted}
    if refs.keys()!=wanted or sources.keys()!=wanted:raise ValueError('Test source/reference mismatch')
    scorer=LocalSComet(COMET,batch_size=8,gpus=1)
    if scorer.identity['sha256']!=COMET_SHA256:raise ValueError('COMET checkpoint changed')
    result={}
    for condition in CONDITIONS:
        jobs=[j for j in load_state(folder/condition)['jobs'] if j['phase']=='target']
        if len(jobs)!=300 or any(j['status'] not in ('complete','failed') for j in jobs):
            raise ValueError('Condition is unfinished: '+condition)
        items=[]; oversize=0
        for j in jobs:
            key=j['source_id'];item={'src':sources[key],'mt':j.get('eval_reply',{}).get('text',''),'ref':refs[key]}
            try: scorer.validate_length(item)
            except CometLengthError:
                item['mt']='';scorer.validate_length(item);oversize+=1
            items.append(item)
        scores=scorer.score(items)
        rows=[{'source_id':j['source_id'],'status':j['status'],'scomet':float(s)} for j,s in zip(jobs,scores)]
        write_json(folder/condition/'scores.json',rows)
        result[condition]={'scomet':mean(scores),'n':len(scores),'failed':sum(j['status']=='failed' for j in jobs),
                           'selector_fallback':sum(j.get('selector_status')=='fallback' for j in jobs),
                           'oversize_as_empty':oversize}
    write_json(folder/'result.json',{'conditions':result,'scorer':scorer.identity})
    print(json.dumps(result,ensure_ascii=False),flush=True)


def progress(folder):
    manifest=json.loads((folder/'manifest.json').read_text())
    print('Naev ablation | 300 targets per condition | seed',manifest['sample_seed'],flush=True)
    for condition in CONDITIONS:
        path=folder/condition
        state=json.loads((path/'state.json').read_text())
        jobs=state['jobs']
        for phase in ('warmup','target'):
            selected=[j for j in jobs if j['phase']==phase]
            counts=Counter(j['status'] for j in selected)
            done=counts['complete']+counts['failed']
            print(f"{condition:8s} {phase:6s} {done:3d}/{len(selected):3d} "
                  f"complete={counts['complete']} failed={counts['failed']} "
                  f"working={counts['working']} | {state['status']}",flush=True)
        report=path/'progress.json'
        if report.exists():
            data=json.loads(report.read_text())
            budget=data['budget']
            print(f"         API calls={budget['calls']} estimated_cost_usd={budget['cost']:.5f} "
                  f"current_task={data.get('current_task') or '-'}",flush=True)
            if data.get('worker_errors'):
                print('         worker_errors:',data['worker_errors'],flush=True)
    result=folder/'result.json'
    if result.exists():print('Scored results:',result,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('stage',choices=('prepare','run','score','progress'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--condition',choices=CONDITIONS)
    a=p.parse_args()
    if a.stage=='prepare':prepare(a.output)
    elif a.stage=='score':score(a.output)
    elif a.stage=='progress':progress(a.output)
    else:
        if not a.condition:p.error('--condition is required')
        run(a.output,a.condition)
