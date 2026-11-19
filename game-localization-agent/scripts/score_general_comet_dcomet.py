"""Official Doc-COMET scoring in its isolated Python 3.9 environment."""

import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
from statistics import mean


CHECKPOINT=os.environ.get('COMET_CHECKPOINT', '/root/autodl-tmp/comet-models/wmt22-comet-da/checkpoints/model.ckpt')
CACHE=os.environ.get('HF_HOME', '/root/autodl-tmp/hf-comet-dpo')
MODELS=('7b','8b','14b')
METHODS=('sentence','doc2doc','framework')
REFERENCES=Path('outputs/multidomain_sft_2026-11-03/offline/references.jsonl')


def load_state(path):
    state=json.loads((path/'state.json').read_text())
    if state.get('storage_format')=='per_job_v1':
        state['jobs']=[json.loads((path/r['checkpoint']).read_text()) if (path/r['checkpoint']).exists() else r
                       for r in state['jobs']]
    return state


def score_dataset(folder, dataset, model, gpus, test_data=None):
    manifest=json.loads((folder/'manifest.json').read_text())
    rows=[r for r in manifest['samples'] if r['dataset']==dataset]
    ids=[r['source_id'] for r in rows]
    if test_data is not None:
        source_rows = {row['source_id']: row for row in test_data[dataset]}
        if set(source_rows) != set(ids):
            raise ValueError('Test IDs differ: ' + dataset)
        sources = {key: row['src'] for key, row in source_rows.items()}
        refs = {key: row['ref'] for key, row in source_rows.items()}
    else:
        refs={}
        with REFERENCES.open(encoding='utf-8') as f:
            for line in f:
                record=json.loads(line)
                if record['source_id'] in ids:refs[record['source_id']]=record['reference_text']
        if refs.keys()!=set(ids):raise ValueError('Reference IDs differ: '+dataset)
        sources={}
        for project in {r['project_id'] for r in rows}:
            db=sqlite3.connect(Path(manifest['graphs'][project]['path']).resolve().as_uri()+'?mode=ro',uri=True)
            try:
                for row in (r for r in rows if r['project_id']==project):
                    sources[row['source_id']]=json.loads(db.execute('SELECT payload FROM sources WHERE project=? AND id=?',
                        (project,row['source_id'])).fetchone()[0])['source']
            finally:db.close()
    sep=model.encoder.tokenizer.sep_token
    result={};all_details={};all_jobs={}
    for model_name in MODELS:
        for method in METHODS:
            state=load_state(folder/model_name/dataset/method)
            jobs={j['source_id']:j for j in state['jobs']}
            if len(state['jobs'])!=len(ids) or jobs.keys()!=set(ids) or any(j['status'] not in ('complete','failed') for j in jobs.values()):
                raise ValueError('Incomplete or mismatched condition: '+model_name+'/'+method)
            all_jobs[(model_name,method)]=jobs
    # Use identical source/reference context and common coverage across all conditions.
    contexts={};skipped=[];reduced=0
    join=lambda parts:(' '+sep+' ').join(parts)
    for i,row in enumerate(rows):
        key=row['source_id'];prev=[]
        for j in range(i-1,max(-1,i-3),-1):
            if rows[j]['unit_id']!=row['unit_id']:break
            prev.insert(0,rows[j]['source_id'])
        while True:
            texts=[join([sources[k] for k in prev]+[sources[key]]),
                   join([refs[k] for k in prev]+[refs[key]])]
            texts.extend(join([refs[k] for k in prev]+[jobs[key].get('eval_reply',{}).get('text','')])
                         for jobs in all_jobs.values())
            if all(len(model.encoder.tokenizer.encode(text,add_special_tokens=True,truncation=False))<=512
                   for text in texts):
                contexts[key]=list(prev);break
            if not prev:
                skipped.append(key);break
            prev.pop(0);reduced+=1
    for model_name in MODELS:
        for method in METHODS:
            jobs=all_jobs[(model_name,method)];items=[];item_ids=[]
            for row in rows:
                key=row['source_id']
                if key not in contexts:continue
                prev=contexts[key]
                items.append({'src':join([sources[k] for k in prev]+[sources[key]]),
                              'mt':join([refs[k] for k in prev]+[jobs[key].get('eval_reply',{}).get('text','')]),
                              'ref':join([refs[k] for k in prev]+[refs[key]])})
                item_ids.append(key)
            if not items:raise ValueError('No valid Doc-COMET inputs')
            values,_=model.predict(items,batch_size=4,gpus=gpus,num_workers=0,progress_bar=True)
            if len(values)!=len(item_ids) or any(not math.isfinite(float(v)) for v in values):
                raise ValueError('Invalid Doc-COMET outputs')
            key=model_name+'/'+dataset+'/'+method
            result[key]=dict(dcomet=mean(values),scored=len(values),total=len(ids),
                             context_reductions=reduced,oversize_unscored=len(skipped),
                             failed=sum(j['status']=='failed' for j in jobs.values()))
            all_details[key]=[dict(source_id=k,score=float(v),status=jobs[k]['status']) for k,v in zip(item_ids,values)]
            print(key,result[key],flush=True)
    return result,all_details,skipped


def main(folder, gpus, test_data=None):
    os.environ.update(HF_HOME=CACHE,HF_HUB_CACHE=CACHE,
        HUGGINGFACE_HUB_CACHE=CACHE,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1')
    from transformers import XLMRobertaModel,XLMRobertaConfig
    # The checkpoint contains the complete encoder; initialize its architecture offline.
    XLMRobertaModel.from_pretrained=classmethod(lambda cls,name,**kw:
        cls(XLMRobertaConfig.from_pretrained(name,local_files_only=True),**kw))
    from comet import load_from_checkpoint
    model=load_from_checkpoint(CHECKPOINT);model.set_document_level()
    results={};all_details={};skipped_by_dataset={}
    for dataset in ('news','guofeng'):
        result,details,skipped=score_dataset(folder,dataset,model,gpus,test_data)
        results.update(result);all_details.update(details);skipped_by_dataset[dataset]=skipped
    details=folder/'dcomet_segment_scores.json';details.write_text(json.dumps(all_details,ensure_ascii=False,indent=2)+'\n')
    value=dict(conditions=results,metric='official Doc-COMET; WMT22 COMET DA checkpoint',
               upstream_commit='5385cc28930aae9924edcb3201645dd3810b12c0',window='previous up to 2 sentences within document',
               contexts='source history for source; reference history for hypothesis and reference, offline only',
               max_tokens=512,overlength='drop oldest context equally across conditions; if any current item remains too long, exclude it from all',
               common_oversize_ids_by_dataset=skipped_by_dataset,
               document_scope='Loong per-document files for News; paired upstream chapter tags for GuoFeng')
    (folder/'dcomet_result.json').write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--gpus', type=int, default=1, choices=(0, 1))
    p.add_argument('--test-data', type=Path)
    a = p.parse_args()
    test_data = None
    if a.test_data:
        import hashlib
        with Path(CHECKPOINT).open('rb') as stream:
            checksum = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(block)
        if checksum.hexdigest() != 'e213091cde220f97b89f8bdfa750c458cfea741ad62affb455b59900210ff2af':
            raise ValueError('WMT22 COMET-DA checkpoint differs from the frozen run')
        test_data = json.loads(a.test_data.read_text())
        METHODS = ('sentence', 'doc2doc', 'framework', 'delta')
    main(a.output, a.gpus, test_data)
