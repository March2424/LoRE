"""Freeze completed DPO rollouts into a validated, provenance-linked dataset."""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path

from localization_agent.selection.preferences import allowed_sources, build_preferences

ROOT=Path.cwd()
GAME=ROOT/'outputs/cpu_preparation_2026-11-02_2000_context_v2'
PUBLIC=ROOT/'outputs/multidomain_sft_2026-11-03'


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage',action='append',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--tokenizer',type=Path,required=True)
    p.add_argument('--cutoff',type=int,default=8192)
    args=p.parse_args();out=args.output.resolve();tmp=out.with_name('.'+out.name+'.preparing')
    if out.exists() or tmp.exists():raise ValueError('Choose a new output path')
    jobs=[];provenance={};stages=[];identities={};policies=set()
    for folder in args.stage:
        folder=folder.resolve();state_path=folder/'state.json';state=json.loads(state_path.read_text())
        if state['status'] not in {'stopped','complete','complete_with_failures','complete_with_warnings'}:
            raise ValueError('Stage must be stopped or completed: '+str(folder))
        counts=collections.Counter();count=0
        for row in state['jobs']:
            if row['phase']!='target':continue
            counts[row['status']]+=1
            if row['status']!='complete':continue
            path=folder/row['checkpoint'];data=path.read_bytes();job=json.loads(data)
            key=(job['project_id'],job['source_id'])
            if key in provenance:raise ValueError('Duplicate completed target: '+str(key))
            provenance[key]={'stage':folder.name,'checkpoint':str(path),'sha256':hashlib.sha256(data).hexdigest()}
            if job.get('scoring'):
                identity=job['scoring']['scorer'];identities[json.dumps(identity,sort_keys=True)]=identity
            policies.add(job['policy_sha256']);jobs.append(job);count+=1
        stages.append({'path':str(folder),'status':state['status'],'state_sha256':sha(state_path),
                       'config_sha256':sha(folder/'config.json'),'target_status':dict(counts),'completed_loaded':count})
    if len(identities)!=1 or len(policies)!=1:raise ValueError('Expected one frozen scorer and selector identity')
    class CachedOnly:
        identity=next(iter(identities.values()))
        def score(self,items):raise ValueError('Missing cached scores; cannot rescore during finalization')
    refs=allowed_sources(GAME,PUBLIC)
    pairs,audit,counts=build_preferences(jobs,refs,CachedOnly(),threshold=0.0)
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(str(args.tokenizer),local_files_only=True)
    for event in audit:event.update(provenance[event['project_id'],event['source_id']])
    for event in audit:
        if event['status']!='pair':continue
        pair=pairs[event['pair_index']]
        prefix='<|im_start|>user\n'+pair['instruction']+'<|im_end|>\n<|im_start|>assistant\n'
        plen=len(tokenizer.encode(prefix,add_special_tokens=False))
        lengths={k:plen+len(tokenizer.encode(pair[k],add_special_tokens=False))+1 for k in ('chosen','rejected')}
        event['sequence_tokens']=lengths
        if max(lengths.values())>args.cutoff:event['status']='excluded_over_cutoff'
    games=sum(e['status']=='pair' and e['group']=='game' for e in audit)
    general=[e for e in audit if e['status']=='pair' and e['group']!='game']
    general_limit=math.floor(games/2)
    for event in general[general_limit:]:event['status']='excluded_by_mix_after_cutoff'
    final=[]
    for event in audit:
        if event['status']=='pair':
            final.append(pairs[event['pair_index']]);event['pair_index']=len(final)-1
        else:event.pop('pair_index',None)
    totals=collections.Counter(e['status'] for e in audit);groups=collections.Counter(e['group'] for e in audit if e['status']=='pair')
    inputs=[GAME/'sources.jsonl',GAME/'splits.jsonl',GAME/'offline/references.jsonl',PUBLIC/'sources_manifest.jsonl',PUBLIC/'offline/references.jsonl',ROOT/'prompts/selection/context_selector.txt',args.tokenizer/'tokenizer.json']
    tmp.mkdir(parents=True)
    def write(name,value): (tmp/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    write('selector_dpo_train.json',final)
    registry=json.loads((ROOT/'configs/training/dataset_info.json').read_text());write('dataset_info.json',{'selector_dpo_train':registry['selector_dpo_train']})
    write('audit.json',audit)
    summary={'stage':'collection_finished_by_user','stages':stages,'total_completed_states':len(jobs),
             'raw_builder_counts':counts,'final_audit_status':dict(totals),'final_pair_groups':dict(groups),
             'final_pairs':len(final),'threshold':0.0,'exact_ties':'excluded','min_game_fraction':2/3,
             'max_sequence_tokens':args.cutoff,'token_count':'qwen3_nothink prompt + response + eos; loss mask still requires trainer validation',
             'selector_policy_sha256':next(iter(policies)),'scorer':CachedOnly.identity,
             'input_sha256':{str(path):sha(path) for path in inputs},
             'warnings':['sCOMET reward is not human gold','reference-driven writeback indirectly influenced historical memory',
                         'GuoFeng TEST_2 is training-seen, not an independent test','fixed-snapshot and sequential-history stages differ',
                         'training loss mask and actual no-truncation need trainer verification']}
    write('summary.json',summary)
    write('sha256.json',{name:sha(tmp/name) for name in ('selector_dpo_train.json','dataset_info.json','audit.json','summary.json')})
    tmp.rename(out)
    print(json.dumps({'output':str(out),'completed_states':len(jobs),'raw_builder_counts':counts,
                      'final_pairs':len(final),'final_groups':dict(groups),'audit_status':dict(totals)},ensure_ascii=False))

if __name__=='__main__':main()
