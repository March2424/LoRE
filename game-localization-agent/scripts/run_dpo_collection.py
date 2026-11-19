"""Prepare, collect, or export frozen-SFT DPO trajectories (external calls only on run)."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import subprocess
from collections import defaultdict
from pathlib import Path

from localization_agent.selection.teacher import prepare, file_hash, read_lines, UncertainCall
from localization_agent.selection.checkpoints import write_json
from localization_agent.selection.dpo_collector import DPOCollector, SFTSelector, export_states

ROOT=Path.cwd()
DATA=ROOT/'outputs/multidomain_sft_2026-11-03'
GAME=ROOT/'outputs/cpu_preparation_2026-11-02_2000_context_v2'
INDEX=ROOT/'outputs/server_stage_inputs_2026-11-03/stage_inputs.json'


def policy_fingerprint(base, adapter):
    index=json.loads((base/'model.safetensors.index.json').read_text())
    paths=[base/'config.json',base/'model.safetensors.index.json',base/'tokenizer.json',
           adapter/'adapter_config.json',adapter/'adapter_model.safetensors',
           ROOT/'prompts/selection/context_selector.txt']
    paths.extend(base/x for x in sorted(set(index['weight_map'].values())))
    return {str(p):file_hash(p) for p in paths}


def pilot_targets(source):
    groups=source['targets'];chosen={'flare':groups['flare'][:48],'shattered':groups['shattered'][:48]}
    for group,count,per in [('news_en_zh',8,4),('news_zh_en',8,4),('guofeng_zh_en',16,8)]:
        by_project=defaultdict(list)
        for row in groups[group]:by_project[row['project_id']].append(row)
        projects=[p for p in sorted(by_project) if len(by_project[p])>=per][:count//per]
        if len(projects)!=count//per:raise ValueError('Pilot cannot cover projects: '+group)
        chosen[group]=[row for project in projects for row in by_project[project][:per]]
    return chosen


class SCometProcess:
    def __init__(self, python, checkpoint, expected_sha, log_path, gpus):
        self.expected_sha=expected_sha;self.identity=None
        self.log=log_path.open('a')
        env=dict(os.environ,HF_HOME=os.environ.get('HF_HOME', '/root/autodl-tmp/hf-comet-dpo'),
                 HF_HUB_CACHE='/root/autodl-tmp/hf-comet-dpo',HUGGINGFACE_HUB_CACHE='/root/autodl-tmp/hf-comet-dpo',
                 HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONPATH=str(ROOT/'src'))
        env.pop('LLM_API_KEY',None)
        self.process=subprocess.Popen([str(python),'-u','scripts/dpo_scorer_worker.py',
            '--game-root',str(GAME),'--public-root',str(DATA),'--checkpoint',str(checkpoint),'--gpus',str(gpus)],
            cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,bufsize=1)
        if not select.select([self.process.stdout],[],[],300)[0]:
            self.close();raise RuntimeError('sCOMET worker initialization timed out')
        line=self.process.stdout.readline()
        if not line:
            self.close();raise RuntimeError('sCOMET worker failed initialization; inspect scorer.log')
        ready=json.loads(line)
        if ready.get('status')!='ready' or ready['identity']['sha256']!=expected_sha:
            self.close();raise RuntimeError('sCOMET worker initialization or checkpoint hash mismatch')
        self.identity=ready['identity']

    def score(self, project_id, source_id, source, translations):
        payload={'project_id':project_id,'source_id':source_id,'source':source,'translations':translations}
        try:
            self.process.stdin.write(json.dumps(payload,ensure_ascii=False)+'\n');self.process.stdin.flush()
            if not select.select([self.process.stdout],[],[],300)[0]:raise TimeoutError('sCOMET worker timed out')
            line=self.process.stdout.readline()
            if not line:raise RuntimeError('sCOMET worker exited')
            result=json.loads(line)
            if result['status']!='ok':raise RuntimeError('sCOMET: '+result.get('error',result['status']))
            identity=result['identity']
            if identity['sha256']!=self.expected_sha:raise RuntimeError('sCOMET checkpoint fingerprint mismatch')
            if self.identity is not None and identity!=self.identity:raise RuntimeError('sCOMET identity changed')
            self.identity=identity
            self.last_device=result.get('scoring_device')
            return result['scores'],identity,result['reference_use']
        except Exception as error:
            raise UncertainCall('Scorer unavailable; inspect scorer.log: '+type(error).__name__+': '+str(error)) from error

    def close(self):
        try:self.process.stdin.close()
        except BrokenPipeError:pass
        try:self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:self.process.terminate();self.process.wait(timeout=10)
        self.log.close()


def prepare_collection(source_plan, output, *, pilot, max_cost, checkpoint, expected_comet_sha256=None):
    if output.exists():raise ValueError('Output exists; use run to resume or choose a new output')
    source=json.loads(source_plan.read_text())
    for name,expected in source['input_sha256'].items():
        if file_hash(ROOT/name)!=expected:raise ValueError('Source plan input changed: '+name)
    selected=pilot_targets(source) if pilot else source['targets']
    by_project=defaultdict(list)
    for rows in selected.values():
        for row in rows:by_project[row['project_id']].append(row['source_id'])
    sources=read_lines(GAME/'sources.jsonl');manifest=read_lines(DATA/'sources_manifest.jsonl')
    allowed=defaultdict(list)
    for row in sources:
        if row['split']=='train' and row['game'] in by_project:allowed[row['game']].append(row['id'])
    for row in manifest:
        if row['project_id'] in by_project and row['sft_allowed'] and row['split'] in {'train','adaptation_seen'}:
            allowed[row['project_id']].append(row['source_id'])
    assets=json.loads(INDEX.read_text());specs={row['project_id']:row for row in assets['public_source_graphs']+assets['game_source_graphs']}
    annotations=json.loads((GAME/'native_bindings.json').read_text())
    annotations.update(json.loads((ROOT/'outputs/server_stage_inputs_2026-11-03/teacher_plan_server/native_bindings.json').read_text()))
    plan=output.parent/(output.name+'-plan')
    if plan.exists():raise ValueError('Plan already exists: '+str(plan))
    plan.mkdir(parents=True)
    sequences=[];splits=[];projects={}
    for project,ids in sorted(by_project.items()):
        warm=source['warmup_ids'][project];visible=set(allowed[project]);spec=specs[project]
        if not set(ids+warm)<=visible or set(ids)&set(warm):raise ValueError('Invalid target/warmup scope: '+project)
        if file_hash(spec['source_db'])!=spec['sha256']:raise ValueError('Source graph changed: '+project)
        projects[project]={'source_db':spec['source_db'],'source_graph_sha256':spec['sha256']}
        sequences.append({'game':project,'split':'train','memory_scope':'game_split','sequence_id':project,
                          'warmup_ids':warm,'target_ids':ids})
        splits.extend({'game':project,'source_id':key,'group_id':project,'split':'train','source_usable':True} for key in sorted(visible))
    def write_lines(path,rows):path.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows))
    write_lines(plan/'pilot_sequences.jsonl',sequences);write_lines(plan/'splits.jsonl',splits)
    write_json(plan/'source_projects.json',projects)
    write_json(plan/'native_bindings.json',{p:annotations.get(p,{}) for p in by_project})
    write_json(plan/'summary.json',{'memory_coverage':{'required_memory_kinds':[]},'source_plan_sha256':file_hash(source_plan)})
    write_json(plan/'graph_config.json',json.loads((ROOT/'configs/training/dpo_multidomain_graph.json').read_text()))
    base=Path(os.environ.get('QWEN_8B', '/root/autodl-tmp/Qwen3-8B'))
    adapter=Path(os.environ.get('SELECTOR_SFT_ADAPTER', ROOT/'outputs/selector_training/sft_multidomain_96gb'))
    progress=json.loads((adapter/'progress.json').read_text())
    if progress['status']!='complete' or progress['global_step']!=progress['max_steps']:raise ValueError('Mixed SFT is not complete')
    fingerprints=policy_fingerprint(base,adapter)
    prepare(output,plan,ROOT/'configs/flare.memory_trial.json',max_cost,max_calls=10000,max_tokens=100000000)
    config=json.loads((output/'config.json').read_text())
    if expected_comet_sha256 and not re.fullmatch(r'[0-9a-f]{64}',expected_comet_sha256):raise ValueError('Invalid expected COMET SHA-256')
    actual_comet_sha256=file_hash(checkpoint) if checkpoint.exists() else None
    if not actual_comet_sha256 and not expected_comet_sha256:raise ValueError('Missing COMET checkpoint and expected SHA-256')
    if actual_comet_sha256 and expected_comet_sha256 and actual_comet_sha256!=expected_comet_sha256:raise ValueError('COMET checkpoint SHA-256 mismatch')
    config.update(collection_stage='dpo_student',policy_base=str(base),policy_adapter=str(adapter),
        policy_files=fingerprints,policy_sha256=hashlib.sha256(json.dumps(fingerprints,sort_keys=True).encode()).hexdigest(),
        source_plan_sha256=file_hash(source_plan),reference_text_access=False,pilot=pilot,
        scorer_checkpoint=str(checkpoint.resolve()),scorer_checkpoint_sha256=actual_comet_sha256 or expected_comet_sha256,
        scorer_pending_at_preparation=actual_comet_sha256 is None,
        memory_selection_policy='highest_scomet_once',dataset_workers=4,http_workers=4,
        dataset_queue_policy='flare_shattered_news_guofeng_serial_v1',
        runtime_scripts={str(ROOT/name):file_hash(ROOT/name) for name in ('scripts/run_dpo_collection.py','scripts/dpo_scorer_worker.py')})
    write_json(output/'config.json',config)
    write_json(output/'frozen-config.json',{'sha256':file_hash(output/'config.json')})
    state=json.loads((output/'state.json').read_text())
    for job in state['jobs']:job['run_id']='dpo_student:'+job['game']+':train'
    write_json(output/'state.json',state)
    print(json.dumps({'status':'prepared_no_paid_calls','projects':len(by_project),'targets':sum(map(len,by_project.values())),
                      'warmup':sum(len(source['warmup_ids'][p]) for p in by_project),'max_cost_usd':max_cost,'output':str(output)}))


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    prep=sub.add_parser('prepare');prep.add_argument('--source-plan',type=Path,required=True);prep.add_argument('--output',type=Path,required=True)
    prep.add_argument('--pilot',action='store_true');prep.add_argument('--max-cost',type=float,required=True);prep.add_argument('--comet-checkpoint',type=Path,required=True);prep.add_argument('--expected-comet-sha256')
    run=sub.add_parser('run');run.add_argument('--output',type=Path,required=True);run.add_argument('--credential-env-file',type=Path);run.add_argument('--comet-python',type=Path,required=True);run.add_argument('--comet-gpus',type=int,choices=(0,1),default=1)
    export=sub.add_parser('export');export.add_argument('--output',type=Path,required=True);export.add_argument('--states',type=Path,required=True)
    args=p.parse_args();output=args.output.resolve()
    if args.command=='prepare':
        if args.max_cost<=0:raise ValueError('Positive cost limit required')
        prepare_collection(args.source_plan.resolve(),output,pilot=args.pilot,max_cost=args.max_cost,checkpoint=args.comet_checkpoint.resolve(),expected_comet_sha256=args.expected_comet_sha256);return
    if args.command=='export':
        if args.states.exists():raise ValueError('Use a new states output')
        print(json.dumps({'states':export_states(output,args.states)}));return
    if (output/'STOP').exists():raise RuntimeError('STOP marker present')
    if args.credential_env_file:
        for line in args.credential_env_file.read_text().splitlines():
            key,sep,value=line.strip().removeprefix('export ').partition('=')
            if sep and key=='LLM_API_KEY':os.environ.setdefault(key,value.strip().strip('\"\''))
    frozen=json.loads((output/'frozen-config.json').read_text())
    if file_hash(output/'config.json')!=frozen['sha256']:raise ValueError('Frozen run configuration changed')
    config=json.loads((output/'config.json').read_text())
    if config.get('collection_stage')!='dpo_student':raise ValueError('Not a DPO collection')
    if not os.environ.get('LLM_API_KEY'):raise RuntimeError('Missing LLM_API_KEY')
    for category in ('prompts','code','assets','policy_files','runtime_scripts'):
        for path,digest in config[category].items():
            if file_hash(path)!=digest:raise ValueError('Frozen '+category+' changed: '+path)
    binding=config['native_bindings'];assert file_hash(binding['path'])==binding['sha256']
    with (output/'.run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if file_hash(config['scorer_checkpoint'])!=config['scorer_checkpoint_sha256']:raise ValueError('COMET checkpoint changed')
        selector=SFTSelector(config['policy_base'],config['policy_adapter'])
        scorer=SCometProcess(args.comet_python,config['scorer_checkpoint'],config['scorer_checkpoint_sha256'],output/'scorer.log',args.comet_gpus)
        try:
            collector=DPOCollector(output,config,json.loads(Path(binding['path']).read_text()),selector,scorer)
            result=collector.run();print(json.dumps({'status':result['status']}))
        finally:scorer.close()


if __name__=='__main__':main()
