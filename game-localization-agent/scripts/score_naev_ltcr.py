"""Score first-mention proper-name consistency across the selected Naev resources."""

import argparse
from collections import Counter, defaultdict
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.preferences import lines


CONDITIONS = ('baseline', 'memory', 'graph', 'selector')
SOURCES = Path('outputs/cpu_preparation_2026-11-02_2000_context_v2/sources.jsonl')
TOOLS = Path(os.environ.get('LTCR_TOOLS', '/root/autodl-tmp/ltcr-eval'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main(folder, conditions=CONDITIONS, expected_count=300, result_directory='ltcr_300_pooled'):
    import jieba
    import spacy
    from awesome_align.tokenization_bert import BertTokenizer

    manifest_path = folder / 'manifest.json'
    ids = [r['source_id'] for r in json.loads(manifest_path.read_text())['samples'] if r['phase']=='target']
    if len(ids)!=expected_count or len(set(ids))!=expected_count:
        raise ValueError(f'Expected {expected_count} distinct target records')
    wanted=set(ids)
    sources={r['id']:r['source_text'] for r in lines(SOURCES) if r['game']=='naev' and r['id'] in wanted}
    if sources.keys()!=wanted:
        raise ValueError('Source IDs do not match the frozen manifest')
    jobs={}
    for condition in conditions:
        state=load_state(folder/condition)
        targets=[x for x in state['jobs'] if x['phase']=='target']
        jobs[condition]={x['source_id']:x for x in targets}
        if len(targets)!=expected_count or jobs[condition].keys()!=wanted or any(x['status'] not in ('complete','failed') for x in targets):
            raise ValueError('Incomplete or mismatched condition: '+condition)

    nlp=spacy.load('en_core_web_sm')
    tokenizer=BertTokenizer.from_pretrained(str(TOOLS/'mbert'))
    records=[]
    name_counts=Counter()
    for key in ids:
        doc=nlp(sources[key])
        tokens=[t for t in doc if not t.is_space]
        spans=[]
        i=0
        while i<len(tokens):
            if tokens[i].pos_!='PROPN':
                i+=1
                continue
            start=i
            while i<len(tokens) and tokens[i].pos_=='PROPN':i+=1
            name=' '.join(t.text for t in tokens[start:i])
            if len(name)>1:spans.append({'name':name,'start':start,'end':i})
        if spans:
            records.append({'id':key,'source_tokens':[t.text for t in tokens], 'spans':spans})
            name_counts.update(x['name'] for x in spans)
    repeated={name for name,count in name_counts.items() if count>1}
    for record in records:
        record['spans']=[x for x in record['spans'] if x['name'] in repeated]
    records=[record for record in records if record['spans']]

    output=folder/result_directory
    output.mkdir(exist_ok=True)
    if (output/'result.json').exists():
        raise ValueError('Existing LTCR result is preserved; select a new output directory')
    details=[]
    results={}
    for condition in conditions:
        work=[]
        skipped=[]
        for record in records:
            job=jobs[condition][record['id']]
            target=job.get('eval_reply',{}).get('text','').strip()
            source_tokens=record['source_tokens']
            target_tokens=[x for x in jieba.cut(target) if x.strip()]
            source_pieces=sum(len(tokenizer.tokenize(x)) for x in source_tokens)
            target_pieces=sum(len(tokenizer.tokenize(x)) for x in target_tokens)
            if (not target_tokens or source_pieces>510 or target_pieces>510 or
                    any('|||' in x for x in source_tokens+target_tokens)):
                skipped.append({'id':record['id'],'reason':'empty, overlong, or delimiter in text'})
                continue
            work.append({**record,'target_tokens':target_tokens})
        pairs=output/(condition+'.pairs.txt')
        pairs.write_text(''.join(' '.join(x['source_tokens'])+' ||| '+' '.join(x['target_tokens'])+'\n' for x in work))
        alignment=output/(condition+'.align.txt')
        env=dict(os.environ, PYTHONPATH=str(TOOLS/'deps')+':'+str(TOOLS/'awesome-align')+':'+os.environ.get('PYTHONPATH',''))
        subprocess.run([os.environ.get('LTCR_PYTHON', sys.executable),'-m','awesome_align.run_align',
                        '--data_file',str(pairs),'--model_name_or_path',str(TOOLS/'mbert'),
                        '--extraction','softmax','--batch_size','8','--output_file',str(alignment)],
                       check=True,env=env)
        aligned=alignment.read_text().splitlines()
        if len(aligned)!=len(work):raise ValueError('Alignment row count changed')
        exact=fuzzy=compared=unaligned=pair_exact=pair_compared=pair_unaligned=0
        names=defaultdict(list)
        align_by_id={row['id']:(row,line) for row,line in zip(work,aligned)}
        for record in records:
            if record['id'] not in align_by_id:
                for mention in record['spans']:
                    names[mention['name']].append((record['id'],''))
                continue
            row,line=align_by_id[record['id']]
            links=defaultdict(set)
            for pair in line.split():
                src,tgt=map(int,pair.split('-'))
                links[src].add(tgt)
            for mention in row['spans']:
                indices=sorted({j for i in range(mention['start'],mention['end']) for j in links[i]})
                value=''.join(row['target_tokens'][j] for j in indices).strip()
                names[mention['name']].append((row['id'],value))
        for name,values in names.items():
            first_id,first=values[0]
            for i,(_,left) in enumerate(values):
                for _,right in values[i+1:]:
                    if left and right:
                        pair_compared+=1
                        pair_exact+=left==right
                    else:
                        pair_unaligned+=1
            for source_id,current in values[1:]:
                item={'condition':condition,'source_id':source_id,'first_source_id':first_id,'source_name':name,
                      'first':first,'current':current,'status':'compared'}
                if not first or not current:
                    unaligned+=1
                    item['status']='unaligned'
                else:
                    compared+=1
                    same=first==current
                    partial=first in current or current in first
                    exact+=same
                    fuzzy+=partial
                    item.update(exact=same,fuzzy=partial)
                details.append(item)
        results[condition]={'ltcr':pair_exact/pair_compared if pair_compared else None,
                            'ltcr_pair_matches':pair_exact,'ltcr_pairs_compared':pair_compared,
                            'ltcr_pairs_unaligned':pair_unaligned,
                            'ltcr_1':exact/compared if compared else None,
                            'ltcr_1f':fuzzy/compared if compared else None,
                            'exact_matches':exact,'fuzzy_matches':fuzzy,'compared':compared,
                            'unaligned_comparisons':unaligned,'skipped_records':len(skipped),
                            'skip_details':skipped}
    write_json(output/'details.json',details)
    result={'scope':f'user-defined pool of {expected_count} source occurrences in frozen experiment order',
            'source_records':expected_count,'records_with_repeated_proper_names':len(records),
            'source_mentions':sum(len(r['spans']) for r in records),
            'spacy_version':spacy.__version__,'spacy_model':'en_core_web_sm 3.8.0',
            'alignment':'awesome-align softmax mBERT','awesome_align_commit':'5f150d45bbe51e167daf0a84abebaeb07c3323d1',
            'source_manifest_sha256':sha(manifest_path),'conditions':results}
    write_json(output/'result.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args().output)
