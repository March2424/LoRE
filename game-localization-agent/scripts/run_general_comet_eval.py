"""Manually run frozen general-domain comparisons; references are offline-only."""

import argparse
from collections import Counter
from dataclasses import asdict
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from localization_agent.memory import MemoryStore
from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.teacher import file_hash
from run_naev_scomet import generate


MODELS = {'7b':'/root/autodl-tmp/Qwen2.5-7B-Instruct',
          '8b':'/root/autodl-tmp/Qwen3-8B',
          '14b':'/root/autodl-tmp/Qwen3-14B/Qwen3-14B'}
STAGES = (('news','sentence'),('news','doc2doc'),('news','framework'),('guofeng','sentence'),
          ('guofeng','doc2doc'),('guofeng','framework'))
PROMPTS = Path('prompts/general_eval')
TRAIN_PY = '/root/autodl-tmp/LLM_Project/.venv-training/bin/python'
GPU_LOCK = Path('outputs/naev_model_compare_100_20261110/.gpu.lock')
CODE = ('scripts/run_general_comet_eval.py','scripts/run_naev_scomet.py',
        'src/localization_agent/selection/collector.py','src/localization_agent/memory/service.py',
        'src/localization_agent/retrieval/service.py',
        'scripts/score_general_comet_dcomet.py')


def model_assets():
    from run_naev_scomet import SFT,DPO
    result={}
    for name,location in MODELS.items():
        path=Path(location)
        result[name]={'config_sha256':file_hash(path/'config.json'),
                      'tokenizer_config_sha256':file_hash(path/'tokenizer_config.json'),
                      'weight_shards':{p.name:p.stat().st_size for p in sorted(path.glob('*.safetensors'))}}
        if not result[name]['weight_shards']:raise ValueError('Missing model weights: '+name)
    result['selector']={'sft_config_sha256':file_hash(SFT/'config.json'),
                        'dpo_adapter_sha256':file_hash(DPO/'adapter_model.safetensors')}
    return result


def setup(folder):
    manifest=json.loads((folder/'manifest.json').read_text())
    base=json.loads(Path('outputs/naev_ablation_300_20261109_manual/selector/config.json').read_text())
    prompts={str(p):file_hash(p) for p in sorted(PROMPTS.rglob('*.txt'))}
    if not prompts:raise ValueError('General-domain prompts are missing')
    config_path=folder/'experiment_config.json'
    if config_path.exists():raise ValueError('Experiment already initialized; preserve existing outputs')
    for spec in manifest['graphs'].values():
        if file_hash(spec['path']) != spec['sha256']:raise ValueError('Frozen source graph changed')
    for model in MODELS:
        for dataset,method in STAGES:
            path=folder/model/dataset/method
            path.mkdir(parents=True)
            sample=[r for r in manifest['samples'] if r['dataset']==dataset]
            jobs=[dict(game=r['project_id'],run_id=f'general:{model}:{dataset}:{method}:{r["unit_id"]}',
                       source_id=r['source_id'],split=r['split'],group_id=r['unit_id'],phase='target',
                       status='pending',trajectory_schema_version=2) for r in sample]
            write_json(path/'state.json',dict(status='prepared',jobs=jobs))
            if method!='framework':continue
            config=dict(base)
            config['memory']=dict(base['memory'],prompt_dir=str(PROMPTS/'memory'))
            config['allowed_source_ids']={p:[r['source_id'] for r in sample if r['project_id']==p]
                                          for p in {r['project_id'] for r in sample}}
            for project in config['allowed_source_ids']:
                src=Path(manifest['graphs'][project]['path'])
                source=sqlite3.connect(src.resolve().as_uri()+'?mode=ro',uri=True)
                store=MemoryStore(path/(project+'.sqlite'))
                try:
                    if source.execute('SELECT count(*) FROM runs').fetchone()[0]:
                        raise ValueError('Source graph contains runtime memory')
                    source.backup(store.db)
                    store.create_snapshot('teacher-knowledge',project)
                finally:
                    source.close();store.close()
            write_json(path/'config.json',config)
            write_json(path/'budget.json',dict(max_cost=3.0,max_calls=1000,max_tokens=3000000))
    write_json(config_path,dict(models=MODELS,model_assets=model_assets(),prompts=prompts,
               code_sha256={p:file_hash(p) for p in CODE},manifest_sha256=file_hash(folder/'manifest.json'),
               doc2doc='whole contiguous chapters; split only when the common model window requires it; reserve 8192 output tokens',
               translator_decoding='greedy; Qwen thinking disabled',
               selector='SFT merged + DPO adapter; generic selection prompt',
               seen='guofeng is selector-training seen',news='Loong per-document IDs, original publication identity unverified'))
    print('Prepared 18 isolated model/dataset/method runs.',flush=True)


def sources(folder, dataset):
    manifest=json.loads((folder/'manifest.json').read_text())
    sample=[r for r in manifest['samples'] if r['dataset']==dataset]
    result={}
    for project in {r['project_id'] for r in sample}:
        spec=manifest['graphs'][project]
        if file_hash(spec['path'])!=spec['sha256']:raise ValueError('Frozen source graph changed: '+project)
        db=sqlite3.connect(Path(spec['path']).resolve().as_uri()+'?mode=ro',uri=True)
        try:
            for row in (r for r in sample if r['project_id']==project):
                result[row['source_id']]=json.loads(db.execute('SELECT payload FROM sources WHERE project=? AND id=?',
                    (project,row['source_id'])).fetchone()[0])['source']
        finally:db.close()
    return sample,result


def verify(folder):
    config=json.loads((folder/'experiment_config.json').read_text())
    if file_hash(folder/'manifest.json')!=config['manifest_sha256']:raise ValueError('Manifest changed')
    for p,h in config['prompts'].items():
        if file_hash(p)!=h:raise ValueError('Frozen prompt changed: '+p)
    for p,h in config['code_sha256'].items():
        if file_hash(p)!=h:raise ValueError('Frozen experiment code changed: '+p)
    if model_assets()!=config['model_assets']:raise ValueError('Frozen model assets changed')


def document_prompt(batch,source):
    items=[{'id':f's{i+1:03d}','source':source[r['source_id']]} for i,r in enumerate(batch)]
    return (PROMPTS/'doc2doc.txt').read_text().format(source_lang=batch[0]['source_lang'],
             target_lang=batch[0]['target_lang'],items=json.dumps(items,ensure_ascii=False))


def chunks(sample, source, tokenizers, windows):
    def fits(batch):
        prompt=document_prompt(batch,source)
        for name,tok in tokenizers.items():
            text=tok.apply_chat_template([{'role':'user','content':prompt}],tokenize=False,
                                          add_generation_prompt=True,enable_thinking=False)
            if len(tok(text,add_special_tokens=False)['input_ids'])+8192>windows[name]:return False
        return True
    batch=[];unit=None
    for row in sample:
        if batch and (row['unit_id']!=unit or not fits(batch+[row])):
            yield batch
            batch=[]
        unit=row['unit_id'];batch.append(row)
        if not fits(batch):raise ValueError('One source item exceeds the common document window')
    if batch:yield batch


def direct(folder,model,dataset,method):
    import torch
    from transformers import AutoConfig,AutoModelForCausalLM, AutoTokenizer
    from localization_agent.models.structured import parse_json_reply
    path=folder/model/dataset/method
    state=json.loads((path/'state.json').read_text())
    lookup={j['source_id']:j for j in state['jobs']}
    sample,source=sources(folder,dataset)
    translator=AutoModelForCausalLM.from_pretrained(MODELS[model],dtype=torch.bfloat16,
        device_map={'':0},attn_implementation='sdpa',local_files_only=True).eval()
    tok=AutoTokenizer.from_pretrained(MODELS[model],local_files_only=True)
    state['status']='running';write_json(path/'state.json',state)
    if method=='sentence':batches=([r] for r in sample)
    else:
        tokenizers={k:(tok if k==model else AutoTokenizer.from_pretrained(v,local_files_only=True))
                    for k,v in MODELS.items()}
        windows={k:AutoConfig.from_pretrained(v,local_files_only=True).max_position_embeddings for k,v in MODELS.items()}
        batches=chunks(sample,source,tokenizers,windows)
    for batch in batches:
        if (path/'STOP').exists():state['status']='stopped';break
        if all(lookup[r['source_id']]['status'] in ('complete','failed') for r in batch):continue
        ids=[r['source_id'] for r in batch]
        if any(lookup[k]['status'] in ('complete','failed') for k in ids):
            raise ValueError('Partial document segment requires manual review')
        if method=='sentence':
            prompt=(PROMPTS/'sentence.txt').read_text().format(source_lang=batch[0]['source_lang'],
                target_lang=batch[0]['target_lang'],source=source[ids[0]])
        else:
            prompt=document_prompt(batch,source)
        for k in ids:lookup[k].update(status='working',translation_prompt=prompt)
        write_json(path/'state.json',state)
        text=''
        try:
            if method=='sentence':
                text,n,m,finished=generate(translator,tok,prompt,selector=False)
                values={ids[0]:text}
            else:
                aliases={f's{i+1:03d}':key for i,key in enumerate(ids)}
                segment=sha256('\n'.join(ids).encode()).hexdigest()[:16]
                segment_dir=path/'segments';segment_dir.mkdir(exist_ok=True)
                for k in ids:lookup[k]['segment_checkpoint']='segments/'+segment+'.json'
                attempts=[]
                for attempt in range(2):
                    actual=prompt if not attempt else prompt+'\nReturn one valid JSON object with exactly the required IDs and a nonempty translation for each.\n'
                    formatted=tok.apply_chat_template([{'role':'user','content':actual}],tokenize=False,
                                                       add_generation_prompt=True,enable_thinking=False)
                    inputs=tok(formatted,add_special_tokens=False,return_tensors='pt').to(translator.device)
                    n=inputs.input_ids.shape[1]
                    with torch.inference_mode():
                        output=translator.generate(**inputs,max_new_tokens=8192,do_sample=False,
                            eos_token_id=tok.eos_token_id,pad_token_id=tok.pad_token_id)
                    tokens=output[0,n:];m=tokens.numel()
                    finished=bool(m and tokens[-1].item()==tok.eos_token_id)
                    text=tok.decode(tokens,skip_special_tokens=True).strip()
                    entry={'prompt':actual,'raw':text,'finished':finished,'input_tokens':n,'output_tokens':m}
                    try:
                        result=parse_json_reply(text)
                        if not finished or not isinstance(result,dict) or result.keys()!=aliases.keys() or \
                                any(not isinstance(result[k],str) or not result[k].strip() for k in aliases):
                            raise ValueError('Document output is unfinished, empty or has incorrect IDs')
                        values={key:result[alias] for alias,key in aliases.items()}
                        entry['status']='valid';attempts.append(entry)
                        write_json(segment_dir/(segment+'.json'),{'source_ids':ids,'attempts':attempts})
                        break
                    except (ValueError,TypeError) as error:
                        entry.update(status='invalid',error=str(error));attempts.append(entry)
                        write_json(segment_dir/(segment+'.json'),{'source_ids':ids,'attempts':attempts})
                        if attempt==1:raise
            if not finished or not isinstance(values,dict) or set(values)!=set(ids) or \
                    any(not isinstance(values[k],str) or not values[k].strip() for k in ids):
                raise ValueError('Unfinished, missing, or misaligned translation output')
            for k in ids:lookup[k].update(status='complete',eval_reply={'text':values[k],
                'model':MODELS[model],'input_tokens':n,'output_tokens':m,'cost':0.0})
        except Exception as error:
            for k in ids:lookup[k].update(status='failed',issues=[type(error).__name__+': '+str(error)],
                                           raw_output=text)
        write_json(path/'state.json',state)
        done=sum(j['status'] in ('complete','failed') for j in state['jobs'])
        print(model,dataset,method,done,'/',len(state['jobs']),flush=True)
    else:state['status']='complete_with_failures' if any(j['status']=='failed' for j in state['jobs']) else 'complete'
    write_json(path/'state.json',state)
    if state['status']=='stopped':raise SystemExit(2)


def framework(folder,model,dataset):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM,AutoTokenizer
    from localization_agent.models.chat_completions import ChatCompletionsModel
    from localization_agent.models.protocols import ModelReply
    from localization_agent.selection.collector import Collector
    from run_naev_scomet import SFT,DPO
    path=folder/model/dataset/'framework'
    conf=json.loads((path/'config.json').read_text())
    if not os.environ.get(conf['models']['translation']['api_key_env']):raise ValueError('Missing memory API credential')
    translator=AutoModelForCausalLM.from_pretrained(MODELS[model],dtype=torch.bfloat16,
        device_map={'':0},attn_implementation='sdpa',local_files_only=True).eval()
    tokenizer=AutoTokenizer.from_pretrained(MODELS[model],local_files_only=True)
    selector=PeftModel.from_pretrained(AutoModelForCausalLM.from_pretrained(SFT,dtype=torch.bfloat16,
        device_map={'':'cpu'},local_files_only=True),DPO,is_trainable=False).eval()
    selector_tokenizer=AutoTokenizer.from_pretrained(SFT,local_files_only=True)

    class GeneralCollector(Collector):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.config['memory']['prompt_dir']=Path(self.config['memory']['prompt_dir'])

        def collect_selection(self,job,retriever,pool,call):
            obs=retriever.selector_observation(pool)
            prompt=(PROMPTS/'selector.txt').read_text().format(observation=json.dumps(obs,ensure_ascii=False))
            job.update(observation=obs,student_prompt=prompt)
            try:
                translator.to('cpu');torch.cuda.empty_cache();selector.to('cuda')
                raw,_,_,finished=generate(selector,selector_tokenizer,prompt,selector=True)
                job['selector_reply']=raw
                if not finished:raise ValueError('unfinished selector output')
                action=json.loads(raw);ids=action['selected_ids']
                if not isinstance(ids,list) or not action['rationale'].strip():raise ValueError('invalid action')
                if retriever.select_context(pool,tuple(ids)).status!='ready':raise ValueError('context not ready')
                job['selector_status']='dpo'
            except (ValueError,KeyError,TypeError,AttributeError) as error:
                job.setdefault('issues',[]).append('selector fallback: '+str(error));job['selector_status']='fallback'
            finally:
                selector.to('cpu');torch.cuda.empty_cache();translator.to('cuda')
            if job['selector_status']=='fallback':
                chosen={c.id for c in retriever.build_context(pool).selected}
                ids=[f'c{i:02d}' for i,c in enumerate((c for c in pool.candidates if not c.mandatory),1) if c.id in chosen]
                action={'rationale':'fixed ranking fallback','selected_ids':ids}
            job.update(action=action,selection_checks_passed=True,
                       selected_contexts=[c for c in obs['candidates'] if c['id'] in ids])

        def call(self,game,job,task,prompt):
            if task!='translate_game':return super().call(game,job,task,prompt)
            if 'eval_reply' not in job:
                text,n,m,finished=generate(translator,tokenizer,prompt,selector=False)
                job.update(eval_reply={'text':text,'model':MODELS[model],'input_tokens':n,'output_tokens':m,'cost':0.0},
                           eval_finished=finished)
                self.checkpoints.save(job)
            if not job['eval_reply']['text'] or not job['eval_finished']:raise ValueError('Local translation unfinished')
            return ModelReply(**job['eval_reply'])

    result=GeneralCollector(path,conf,{},ChatCompletionsModel,1,1).run()
    if result['status'] not in ('complete','complete_with_warnings','complete_with_failures'):raise SystemExit(2)


def run(folder,model):
    verify(folder)
    launcher=folder/model/'launcher.json'
    with (folder/model/'.launcher.lock').open('a') as own:
        fcntl.flock(own,fcntl.LOCK_EX|fcntl.LOCK_NB)
        write_json(launcher,dict(status='waiting_for_gpu',pid=os.getpid()))
        print(model,'waiting for GPU lock',flush=True)
        try:
            with GPU_LOCK.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                write_json(launcher,dict(status='running',pid=os.getpid()))
                for dataset,method in STAGES:
                    path=folder/model/dataset/method
                    if json.loads((path/'state.json').read_text())['status'] in ('complete','complete_with_failures','complete_with_warnings'):
                        continue
                    print('Starting',model,dataset,method,flush=True)
                    subprocess.run([TRAIN_PY,__file__,'worker','--output',str(folder),'--model',model,
                                    '--dataset',dataset,'--method',method],check=True,env=os.environ.copy())
            write_json(launcher,dict(status='complete',pid=os.getpid()))
        except Exception as error:
            write_json(launcher,dict(status='failed',pid=os.getpid(),error=type(error).__name__+': '+str(error)))
            raise


def progress(folder):
    for model in MODELS:
        launcher=folder/model/'launcher.json'
        if launcher.exists():
            info=json.loads(launcher.read_text())
            alive=Path('/proc')/str(info['pid'])
            print(f"{model} launcher: {info['status']} pid={info['pid']} alive={alive.exists()}")
        for dataset,method in STAGES:
            state=load_state(folder/model/dataset/method)
            jobs=state['jobs'];counts=Counter(j['status'] for j in jobs)
            fallback=sum(j.get('selector_status')=='fallback' for j in jobs)
            print(f"{model:3} {dataset:7} {method:9} {counts['complete']+counts['failed']:3}/{len(jobs):3} "
                  f"failed={counts['failed']} fallback={fallback} {state['status']}")


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage',choices=('setup','run','worker','progress'))
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model',choices=tuple(MODELS))
    p.add_argument('--dataset',choices=('news','guofeng'))
    p.add_argument('--method',choices=('sentence','doc2doc','framework'))
    a=p.parse_args()
    if a.stage=='setup':setup(a.output)
    elif a.stage=='progress':progress(a.output)
    elif a.stage=='run':
        if not a.model:p.error('--model is required')
        run(a.output,a.model)
    else:
        if not a.model or (a.dataset,a.method) not in STAGES:p.error('Valid --model, --dataset and --method are required')
        verify(a.output)
        (framework if a.method=='framework' else direct)(a.output,a.model,a.dataset,*(() if a.method=='framework' else (a.method,)))
