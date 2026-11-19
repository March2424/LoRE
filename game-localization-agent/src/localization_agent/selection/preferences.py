"""Build sCOMET preferences from frozen, already translated student rollouts."""
import hashlib
import json
import math
from collections import Counter, defaultdict
import random
from pathlib import Path
from importlib.metadata import version


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def lines(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def allowed_sources(game_root, public_root):
    """Resolve references only from the project's explicitly allowed train records."""
    game_root, public_root = Path(game_root), Path(public_root)
    sources = {(x['game'], x['id']): x['source_text'] for x in lines(game_root/'sources.jsonl') if x['split']=='train'}
    allowed = {(x['game'], x['source_id']) for x in lines(game_root/'splits.jsonl')
               if x['split']=='train' and x['score_candidate'] and x['game'] in {'flare','shattered'}}
    records = {}
    for x in lines(game_root/'offline/references.jsonl'):
        key=(x['game'],x['record_id'])
        if x['split']=='train' and key in allowed and key in sources and x['reference_text'].strip():
            if key in records: raise ValueError('Duplicate game reference: '+str(key))
            records[key]={'source':sources[key], 'reference':x['reference_text'], 'group':'game'}
    public = {(x['project_id'],x['source_id']):x for x in lines(public_root/'sources_manifest.jsonl')
              if x['sft_allowed'] and ((x['split']=='train' and x['corpus']=='news_commentary_v18.1') or
                  (x['split']=='adaptation_seen' and x['corpus']=='guofeng_v1_test2' and
                   x['source_lang']=='zh' and x['target_lang']=='en'))}
    for x in lines(public_root/'offline/references.jsonl'):
        key=(x['project_id'],x['source_id'])
        if key not in public or not x['reference_text'].strip(): continue
        source=public[key]
        news=source['corpus']=='news_commentary_v18.1'
        # User explicitly reclassified GuoFeng TEST_2 as train/seen for DPO rewards on 2026-11-03.
        # Existing frozen source/reference manifests are not rewritten.
        if (x['source_lang'],x['target_lang'])!=(source['source_lang'],source['target_lang']):
            raise ValueError('Reference direction differs from source manifest: '+str(key))
        if news and (x['split']!='train' or not x['dpo_reference_allowed']): continue
        if not news and x['split']!='adaptation_seen': continue
        if key in records: raise ValueError('Duplicate public reference: '+str(key))
        records[key]={'source_hash':source['source_hash'], 'reference':x['reference_text'],
                      'group':('news_'+source['source_lang']+'_'+source['target_lang']) if news else 'guofeng_zh_en',
                      'reference_use':'train' if news else 'user_reclassified_train_seen_2026-11-03'}
    return records


def build_preferences(states, references, scorer, *, threshold=0.0, min_game_fraction=2/3, max_pairs=3300, seed=20261103, prompt_path='prompts/selection/context_selector.txt'):
    """Score one frozen state at a time, preserving rejects and never exporting references."""
    if not math.isfinite(threshold) or threshold < 0: raise ValueError('threshold must be nonnegative')
    if not 0 < min_game_fraction <= 1: raise ValueError('game fraction must be in (0, 1]')
    if max_pairs < 1: raise ValueError('max_pairs must be positive')
    template=Path(prompt_path).read_text(); pairs=[]; audit=[]; totals=Counter(); seen=set()
    policy=None
    for state in states:
        if policy is None: policy=state['policy_sha256']
        if not policy or state['policy_sha256']!=policy: raise ValueError('Mixed or missing frozen SFT policies')
        if len(state['attempts'])>4 or len({a['attempt_id'] for a in state['attempts']})!=len(state['attempts']):
            raise ValueError('At most four uniquely identified action attempts are allowed')
        graph=state['graph_config']
        if graph['max_soft_tokens']!=2048 or graph['max_characters']!=12000 or graph['max_selected']!=8 or graph['max_candidates']!=16:
            raise ValueError('Wrong frozen retrieval budget')
        key=(state['project_id'],state['source_id']); totals['states']+=1
        if key in seen: raise ValueError('Duplicate source state: '+str(key))
        seen.add(key)
        event={'project_id':key[0], 'source_id':key[1], 'state_hash':state['state_hash'],
               'pool_hash':state['pool_hash'],'policy_sha256':state['policy_sha256'],'attempts':[]}
        audit.append(event)
        ref=references.get(key)
        if ref is None:
            event['status']='outside_allowed_train_or_missing_reference'; totals[event['status']]+=1; continue
        obs=state['observation']; src=obs['source']
        if (obs['source_id']!=key[1] or obs['pool_hash']!=state['pool_hash'] or
            digest(src)!=ref.get('source_hash',digest(ref.get('source',''))) or
            state['instruction']!=template.format(observation=json.dumps(obs,ensure_ascii=False))):
            raise ValueError('Frozen source, pool or prompt does not match: '+str(key))
        if obs['soft_token_budget']!=2048 or obs['max_selected']!=8:
            raise ValueError('Expected frozen 2048-token and 8-item selection contract')
        candidates=obs['candidates']
        if len(candidates)>16: raise ValueError('Candidate pool exceeds 16')
        legal={x['id'] for x in candidates}
        if len(legal)!=len(candidates): raise ValueError('Duplicate observation candidate ID')
        accepted=[]; distinct=set()
        for attempt in state['attempts']:
            record={'attempt_id':attempt['attempt_id'],'context_hash':attempt.get('context_hash'),
                    'action_sha256':digest(attempt['completion']) if isinstance(attempt.get('completion'),str) else None,
                    'translation_sha256':digest(attempt['translation']) if isinstance(attempt.get('translation'),str) else None};event['attempts'].append(record)
            try:
                action=json.loads(attempt['completion']); ids=action['selected_ids']; rationale=action['rationale']
                if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids) or len(ids)!=len(set(ids)) or len(ids)>8 or not set(ids)<=legal or not isinstance(rationale,str) or not rationale.strip():
                    raise ValueError('invalid_action')
                if attempt['policy_sha256']!=state['policy_sha256'] or attempt['pool_hash']!=state['pool_hash']:
                    raise ValueError('mismatched_frozen_policy_or_pool')
                if attempt.get('finish_reason')!='stop': raise ValueError('truncated_or_unfinished')
                if attempt.get('score_status')=='too_long': raise ValueError('scorer_input_too_long')
                validation=attempt['validation']; translation=attempt['translation']
                if not isinstance(translation,str) or not translation.strip(): raise ValueError('missing_translation')
                # Upstream collector must use GraphRetriever on the frozen pool, then bind its checks to the exact action/context/translation.
                if (validation['status']!='passed' or not all(validation['checks'].get(k) is True for k in ('action_valid','context_budget_passed','translation_checks_passed')) or
                    validation['state_hash']!=state['state_hash'] or validation['source_sha256']!=digest(src) or
                    not attempt['context_hash'] or validation['pool_hash']!=state['pool_hash'] or
                    validation['action_sha256']!=digest(attempt['completion']) or
                    validation['translation_sha256']!=digest(translation) or
                    validation['context_hash']!=attempt['context_hash'] or
                    validation['max_soft_tokens']!=2048 or validation['max_characters']!=12000):
                    raise ValueError('missing_or_stale_validation')
                action_set=tuple(sorted(ids))
                if action_set in distinct: raise ValueError('duplicate_action')
                distinct.add(action_set); accepted.append((attempt,record,translation))
                record['status']='eligible'
            except (KeyError,TypeError,ValueError,json.JSONDecodeError) as error:
                record['status']=str(error) or 'invalid';totals['rejected_attempts']+=1
        if len(accepted)<2:
            event['status']='insufficient_valid_actions';totals[event['status']]+=1;continue
        instances=[{'src':src,'mt':translation,'ref':ref['reference']} for _,_,translation in accepted]
        cached=state.get('scoring')
        if cached:
            if cached['scorer']!=scorer.identity: raise ValueError('Scoring model identity differs from memory-writeback scorer')
            bound={x['attempt_id']:(x['translation_sha256'],score) for x,score in zip(cached['attempts'],cached['scores'])}
            if len(bound)!=len(cached['attempts']) or len(cached['attempts'])!=len(cached['scores']):raise ValueError('Invalid cached scoring')
            scores=[]
            for attempt,_,translation in accepted:
                item=bound.get(attempt['attempt_id'])
                if not item or item[0]!=digest(translation) or item[1] is None:raise ValueError('Stale cached score')
                scores.append(item[1])
        else:
            try:
                scores=scorer.score(instances)
            except CometLengthError as error:
                event.update(status='scorer_input_too_long',error=str(error));totals['scorer_input_too_long']+=1;continue
        if len(scores)!=len(instances) or any(not math.isfinite(float(x)) for x in scores):
            raise ValueError('Scorer returned missing or non-finite segment scores')
        for (_,record,_),score in zip(accepted,scores):record['scomet']=float(score)
        ranked=sorted(range(len(scores)),key=lambda i:float(scores[i]))
        lo,hi=ranked[0],ranked[-1];delta=float(scores[hi])-float(scores[lo])
        event.update(group=ref['group'],reference_use=ref.get('reference_use','train'),score_delta=delta,scorer=scorer.identity)
        if delta == 0 or delta < threshold:
            event['status']='tie';totals['tie']+=1;continue
        chosen,rejected=accepted[hi][0],accepted[lo][0]
        pairs.append({'instruction':state['instruction'],'input':'','chosen':chosen['completion'],'rejected':rejected['completion']})
        event.update(status='pair',pair_index=len(pairs)-1,chosen_attempt=chosen['attempt_id'],rejected_attempt=rejected['attempt_id'],
                     policy_sha256=state['policy_sha256'],pool_hash=state['pool_hash'])
        totals['pairs']+=1;totals['pairs_'+ref['group']]+=1
    # Preserve the 3000-pair game/news plan and add up to 300 GuoFeng seen pairs.
    game=[e for e in audit if e['status']=='pair' and e['group']=='game']
    news=[e for e in audit if e['status']=='pair' and e['group'].startswith('news_')]
    guofeng=[e for e in audit if e['status']=='pair' and e['group']=='guofeng_zh_en']
    rng=random.Random(seed)
    for group in (game,news,guofeng): rng.shuffle(group)
    chosen_game=game[:min(2250,max_pairs)]
    general_cap=min(max_pairs-len(chosen_game), math.floor(len(chosen_game)*(1-min_game_fraction)/min_game_fraction+1e-9),1050)
    news_count=min(len(news),750,math.floor(general_cap*750/1050))
    guofeng_count=min(len(guofeng),300,general_cap-news_count)
    spare=general_cap-news_count-guofeng_count
    extra=min(spare,len(news)-news_count,750-news_count);news_count+=extra;spare-=extra
    guofeng_count+=min(spare,len(guofeng)-guofeng_count,300-guofeng_count)
    chosen_news=news[:news_count];chosen_guofeng=guofeng[:guofeng_count]
    selected={id(e) for e in chosen_game+chosen_news+chosen_guofeng}
    for event in game+news+guofeng:
        if id(event) not in selected: event['status']='excluded_by_mix_or_limit'
    kept=[e for e in audit if e['status']=='pair'];old_pairs=pairs;pairs=[]
    for event in kept:
        pairs.append(old_pairs[event['pair_index']]);event['pair_index']=len(pairs)-1
    for event in game+news+guofeng:
        if event['status']!='pair': event.pop('pair_index',None)
    totals['valid_pairs_before_balance']=totals['pairs'];totals['pairs']=len(pairs)
    totals['excluded_by_mix_or_limit']=len(game)+len(news)+len(guofeng)-len(pairs)
    totals['retained_game_pairs']=len(chosen_game);totals['retained_news_pairs']=len(chosen_news)
    totals['retained_guofeng_pairs']=len(chosen_guofeng)
    return pairs,audit,dict(totals)


def game_context_priority(row):
    """Rank potential context-bearing text without consulting target references."""
    import re
    kind=row.get('text_type','unknown');text=row.get('source_text','').strip()
    words=re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?|[\u4e00-\u9fff]",text)
    if kind.endswith('_name') or kind in {'item_property','credits'} or len(words)<5:return 2
    if kind in {'dialogue','dialogue_option','narrative','quest_description','book_text'}:return 0
    if kind in {'item_description','skill_description'} and len(words)>=8:return 0
    return 1


def plan_sources(game_root, public_root, *, seed=20261103, warmup=10):
    """Select train-only target IDs and disjoint per-project warmup IDs without reading references into a model."""
    game_root,public_root=Path(game_root),Path(public_root)
    refs=allowed_sources(game_root,public_root)
    all_ids=defaultdict(set);game_rows={}
    for row in lines(game_root/'sources.jsonl'):
        if row['split']=='train' and row['game'] in {'flare','shattered'}:
            all_ids[row['game']].add(row['id']);game_rows[(row['game'],row['id'])]=row
    for row in lines(public_root/'sources_manifest.jsonl'):
        if row['sft_allowed'] and ((row['split']=='train' and row['corpus']=='news_commentary_v18.1') or (row['split']=='adaptation_seen' and row['corpus']=='guofeng_v1_test2')):
            all_ids[row['project_id']].add(row['source_id'])
    groups=defaultdict(list)
    for key,row in refs.items():
        group=key[0] if row['group']=='game' else row['group']
        groups[group].append(key)
    quotas={'flare':2250,'shattered':2250,'news_en_zh':750,'news_zh_en':750,'guofeng_zh_en':600}
    rng=random.Random(seed);selected=defaultdict(list); chosen=defaultdict(set); counts={}
    for group,quota in quotas.items():
        pool=sorted(groups[group]);rng.shuffle(pool)
        if group in {'flare','shattered'}:
            pool.sort(key=lambda key:game_context_priority(game_rows[key]))
        for project,source_id in pool:
            if len(selected[group])>=quota:break
            if len(chosen[project])>=max(0,len(all_ids[project])-warmup):continue
            selected[group].append({'project_id':project,'source_id':source_id});chosen[project].add(source_id)
        counts[group]=len(selected[group])
    warmups={}
    for project,targets in sorted(chosen.items()):
        candidates=sorted(all_ids[project]-targets);rng.shuffle(candidates)
        warmups[project]=candidates[:warmup]
        if len(warmups[project])!=warmup: raise ValueError('Insufficient disjoint warmup: '+project)
    return {'status':'planned_no_sampling','seed':seed,'warmup_per_project':warmup,'requested_targets':quotas,
            'actual_targets':counts,'targets':dict(selected),'warmup_ids':warmups,
            'references_in_plan':False,'game_sampling_policy':'context_priority_heuristic_v1','note':'Capacities and reference quality need pilot review; no rollouts or pairs generated.'}


class CometLengthError(ValueError):
    pass


class LocalSComet:
    """Loong's segment-level src/mt/ref scoring contract, in an isolated COMET environment."""
    def __init__(self, checkpoint, *, batch_size=16, gpus=0):
        from comet import load_from_checkpoint
        self.checkpoint=Path(checkpoint).resolve()
        if not self.checkpoint.is_file(): raise ValueError('Provide a local frozen COMET checkpoint')
        h=hashlib.sha256()
        with self.checkpoint.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        self.identity={'checkpoint':str(self.checkpoint),'sha256':h.hexdigest(),'unbabel_comet_version':version('unbabel-comet')}
        self.model=load_from_checkpoint(str(self.checkpoint));self.batch_size=batch_size;self.gpus=gpus
        if not self.model.requires_references(): raise ValueError('A reference-based COMET model is required')
        self.tokenizer=self.model.encoder.tokenizer
        self.identity['hparams_sha256']=hashlib.sha256((self.checkpoint.parents[1]/'hparams.yaml').read_bytes()).hexdigest()
        self.identity['tokenizer_sha256']=hashlib.sha256(self.tokenizer.backend_tokenizer.to_str().encode()).hexdigest()
        self.identity['transformers_version']=version('transformers')
        # The referenced Loong fork uses max_positions - 2. Enforce this conservative bound before scoring.
        self.max_length=int(self.model.encoder.max_positions)-2
        self.identity['max_segment_tokens']=self.max_length

    def validate_length(self, item):
        for field in ('src','mt','ref'):
            ids=self.tokenizer(item[field],add_special_tokens=True,truncation=False)['input_ids']
            if len(ids)>self.max_length:
                raise CometLengthError('COMET '+field+' exceeds untruncated segment limit; inspect this state')

    def score(self, instances):
        for item in instances:self.validate_length(item)
        result=self.model.predict(instances,batch_size=self.batch_size,gpus=self.gpus)
        return list(result['scores'])
