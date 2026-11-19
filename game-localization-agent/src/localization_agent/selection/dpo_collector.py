"""Frozen SFT selector rollouts, independent scoring and highest-score memory."""
import fcntl
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from localization_agent.models.chat_completions import BudgetExceeded, ChatCompletionsModel
from localization_agent.schemas.memory import Occurrence, ReadView, TranslationCheck, text_hash
from .collector import Collector
from .teacher import UncertainCall, CallFailed, translation_issues
from .checkpoints import load_state


class SFTSelector:
    def __init__(self, base, adapter):
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch=torch
        self.tokenizer=AutoTokenizer.from_pretrained(base,local_files_only=True)
        model=AutoModelForCausalLM.from_pretrained(base,torch_dtype=torch.bfloat16,device_map='auto',local_files_only=True)
        self.model=PeftModel.from_pretrained(model,adapter,is_trainable=False).eval()

    def sample(self, instruction, seed):
        prefix='<|im_start|>user\n'+instruction+'<|im_end|>\n<|im_start|>assistant\n'
        ids=self.tokenizer.encode(prefix,add_special_tokens=False)
        if len(ids)+1024>8192: raise ValueError('Complete selector prompt plus 1024 output exceeds 8192')
        self.torch.manual_seed(seed);self.torch.cuda.manual_seed_all(seed)
        inputs=self.torch.tensor([ids],device=self.model.device)
        with self.torch.inference_mode():
            output=self.model.generate(inputs,max_new_tokens=1024,do_sample=True,temperature=0.7,top_p=0.9,
                pad_token_id=self.tokenizer.pad_token_id,eos_token_id=self.tokenizer.eos_token_id)
        new=output[0,len(ids):].tolist()
        return self.tokenizer.decode(new,skip_special_tokens=True), ('stop' if new and new[-1]==self.tokenizer.eos_token_id else 'length')

    def release_gpu_cache(self):
        self.torch.cuda.empty_cache()

    def sample_batch(self, instruction, seed, count):
        prefix='<|im_start|>user\n'+instruction+'<|im_end|>\n<|im_start|>assistant\n'
        ids=self.tokenizer.encode(prefix,add_special_tokens=False)
        if len(ids)+1024>8192:raise ValueError('Complete selector prompt plus 1024 output exceeds 8192')
        inputs=self.torch.tensor([ids],device=self.model.device)
        for size in range(count,0,-1):
            self.torch.manual_seed(seed);self.torch.cuda.manual_seed_all(seed)
            try:
                with self.torch.inference_mode():
                    output=self.model.generate(inputs,max_new_tokens=1024,do_sample=True,temperature=0.7,top_p=0.9,
                        num_return_sequences=size,pad_token_id=self.tokenizer.pad_token_id,eos_token_id=self.tokenizer.eos_token_id)
                break
            except self.torch.cuda.OutOfMemoryError:
                self.torch.cuda.empty_cache()
                if size==1:raise
        results=[]
        for row in output:
            new=row[len(ids):].tolist()
            results.append((self.tokenizer.decode(new,skip_special_tokens=True),
                            'stop' if self.tokenizer.eos_token_id in new else 'length'))
        return results


class DPOCollector(Collector):
    def __init__(self, folder, config, annotations, selector, scorer, factory=ChatCompletionsModel):
        super().__init__(folder,config,annotations,factory,game_workers=4,http_workers=4)
        self.selector=selector
        self.scorer=scorer
        self.selector_lock=threading.Lock()
        self.scorer_lock=threading.Lock()
        self.memory_write_locks={game:threading.Lock() for game in ('flare','shattered')}
        self.state['execution'].update(collection_stage='dpo_student',translation_context_policy='highest_scomet_once',candidate_translation_pipeline=True,selector_batch_size=4,selector_sampling_mode='batch4_v1')

    @staticmethod
    def dataset_group(project):
        if project in {'flare','shattered'}:return project
        if project.startswith('news_commentary_v18.1.'):return 'news_commentary'
        if project.startswith('guofeng_v1_test2.'):return 'guofeng'
        raise ValueError('Unknown DPO dataset: '+project)

    def schedule_jobs(self, workers, groups):
        if self.config.get('fixed_snapshot_workers_per_game'):
            size=self.config['fixed_snapshot_workers_per_game'];partitions=defaultdict(list)
            for game,jobs in groups.items():
                if game not in {'flare','shattered'}:raise ValueError('Fixed snapshot stage only supports selected games')
                for job in jobs:
                    partition=int(text_hash(job['source_id'])[:8],16)%size
                    partitions[(game,partition)].append(job)
            return [workers.submit(self.game,game,jobs,game+':worker:'+str(i)) for (game,i),jobs in sorted(partitions.items())]
        datasets=defaultdict(list)
        for project,jobs in groups.items():datasets[self.dataset_group(project)].append((project,jobs))
        def dataset_queue(projects):
            for project,jobs in projects:
                if self.stop.is_set():break
                if self.game(project,jobs)!='complete':break
        return [workers.submit(dataset_queue,projects) for projects in datasets.values()]

    def process_job(self, job, store, memory, retriever, session, call):
        payload=store.db.execute('SELECT payload FROM sources WHERE project=? AND id=? ORDER BY version DESC LIMIT 1',
                                 (job['game'],job['source_id'])).fetchone()
        source=Occurrence(**json.loads(payload[0]))
        job.update(source=source.source,status='working');job.setdefault('issues',[])
        fixed=self.config.get('snapshot_views',{}).get(job['game'])
        if fixed and fixed['run_id']!=job['run_id']:raise ValueError('Snapshot run mismatch')
        job.setdefault('view',fixed or asdict(store.view(job['run_id'])))
        if fixed and job['view']!=fixed:raise ValueError('Snapshot view changed')
        self.checkpoints.save(job); started=time.monotonic()
        try:
            pool=retriever.retrieve(source,ReadView(**job['view']),'graph')
            if job['phase']=='target':
                obs=retriever.selector_observation(pool)
                prompt=Path('prompts/selection/context_selector.txt').read_text().format(observation=json.dumps(obs,ensure_ascii=False))
                state_hash=text_hash(json.dumps({'project':job['game'],'view':job['view'],'pool_hash':obs['pool_hash'],
                    'policy':self.config['policy_sha256'],'graph':self.config['graph']},sort_keys=True,ensure_ascii=False))
                if job.get('state_hash') and job['state_hash']!=state_hash: raise ValueError('Frozen state changed on resume')
                job.update(project_id=job['game'],state_hash=state_hash,pool_hash=obs['pool_hash'],observation=obs,
                    instruction=prompt,policy_sha256=self.config['policy_sha256'],graph_config=self.config['graph'])
                attempts=job.setdefault('attempts',[]);self.checkpoints.save(job)
                pending={}
                def resolve(index):
                    attempt,pack,future=pending.pop(index)
                    try:
                        translation=future.result().text.strip()
                        problems=translation_issues(source,translation,pack,session)
                        if not translation or problems:raise ValueError('translation_checks_failed: '+str(problems))
                        attempt.update(translation=translation,status='complete',validation={
                            'status':'passed','checks':{'action_valid':True,'context_budget_passed':True,'translation_checks_passed':True},
                            'state_hash':state_hash,'pool_hash':obs['pool_hash'],'context_hash':pack.context_hash,
                            'source_sha256':text_hash(source.source),'action_sha256':text_hash(attempt['completion']),
                            'translation_sha256':text_hash(translation),'max_soft_tokens':2048,'max_characters':12000})
                    except (BudgetExceeded,UncertainCall):raise
                    except (ValueError,KeyError,TypeError,CallFailed) as error:
                        attempt.update(status='invalid',error=type(error).__name__+': '+str(error))
                    finally:self.checkpoints.save(job)
                with ThreadPoolExecutor(max_workers=4,thread_name_prefix='dpo-translation') as workers:
                    for index in range(4):
                        if index>=len(attempts):
                            batch_size=min(4,4-index)
                            seed=int(text_hash(state_hash+':batch4-v1:'+str(index))[:8],16)
                            with self.selector_lock:
                                if hasattr(self.selector,'sample_batch'):
                                    results=self.selector.sample_batch(prompt,seed,batch_size)
                                else:
                                    results=[self.selector.sample(prompt,seed+i) for i in range(batch_size)]
                            if not 1<=len(results)<=batch_size:raise ValueError('Selector batch output count mismatch')
                            for offset,(completion,finish) in enumerate(results):
                                attempts.append({'attempt_id':str(index+offset),'seed':seed,'batch_offset':offset,
                                    'sampling_mode':'batch4_v1','completion':completion,'finish_reason':finish,
                                    'pool_hash':obs['pool_hash'],'policy_sha256':self.config['policy_sha256'],'status':'sampled'})
                            self.checkpoints.save(job)
                        attempt=attempts[index]
                        if attempt['status'] in {'complete','invalid'}:continue
                        try:
                            if attempt['finish_reason']!='stop':raise ValueError('truncated_action')
                            action=json.loads(attempt['completion']);ids=action['selected_ids']
                            if not isinstance(action.get('rationale'),str) or not action['rationale'].strip() or not isinstance(ids,list) or any(not isinstance(x,str) for x in ids):raise ValueError('invalid_action')
                            action_set=sorted(ids)
                            for earlier in list(pending):
                                if attempts[earlier].get('selected_ids')==action_set:resolve(earlier)
                            if any(a.get('selected_ids')==action_set and a['status']=='complete' for a in attempts[:index]):raise ValueError('duplicate_action')
                            pack=retriever.select_context(pool,tuple(ids))
                            if pack.status!='ready':raise ValueError('context_'+pack.status)
                            attempt.update(selected_ids=action_set,context_hash=pack.context_hash,context=asdict(pack));self.checkpoints.save(job)
                            translation_prompt=(memory.config.prompt_dir/'translate_game.txt').read_text().format(
                                source=source.source,source_lang=source.source_lang,target_lang=source.target_lang,
                                contexts=pack.model_context,source_context=pack.manifest['current_source_context'])
                            attempt['translation_prompt']=translation_prompt;self.checkpoints.save(job)
                            pending[index]=(attempt,pack,workers.submit(call,'translate_game',translation_prompt))
                        except (BudgetExceeded,UncertainCall):raise
                        except (ValueError,KeyError,TypeError,CallFailed) as error:
                            attempt.update(status='invalid',error=type(error).__name__+': '+str(error))
                        self.checkpoints.save(job)
                    for index in list(pending):resolve(index)
            if job['phase']=='target':
                job['selection_checks_passed']=sum(a['status']=='complete' for a in job['attempts'])>=2
                self.checkpoints.save(job)
            if job['phase']=='target':
                valid=[a for a in job['attempts'] if a['status']=='complete']
                if not valid:
                    job.update(status='complete',memory_status='no_scored_translation',score_status='no_valid_translation')
                    return None
                current=[{'attempt_id':a['attempt_id'],'translation_sha256':text_hash(a['translation'])} for a in valid]
                if 'scoring' not in job:
                    # Release unused selector allocations and prevent simultaneous GPU generation
                    # while the separate COMET process needs memory on the same device.
                    with self.selector_lock:
                        if hasattr(self.selector,'release_gpu_cache'):
                            self.selector.release_gpu_cache()
                        with self.scorer_lock:
                            scores,identity,reference_use=self.scorer.score(job['game'],source.occurrence_id,source.source,[a['translation'] for a in valid])
                    if len(scores)!=len(valid):raise UncertainCall('sCOMET score count differs from candidate count')
                    job['scoring']={'attempts':current,'scores':scores,'scorer':identity,'reference_use':reference_use,
                                    'compute_device':getattr(self.scorer,'last_device',None)}
                    for attempt,score in zip(valid,scores):attempt['score_status']='scored' if score is not None else 'too_long'
                    self.checkpoints.save(job)
                scoring=job['scoring']
                if scoring['scorer']!=self.scorer.identity:raise ValueError('Scoring environment changed on resume')
                if scoring['attempts']!=current:raise ValueError('Scored translations changed on resume')
                eligible=[i for i,score in enumerate(scoring['scores']) if score is not None]
                job['selection_checks_passed']=len(eligible)>=2
                if not eligible:
                    job.update(status='complete',memory_status='no_scored_translation',score_status='no_scorable_translation');job['issues'].append('No candidate can be scored')
                    return None
                winner=max(eligible,key=lambda i:scoring['scores'][i])
                selected=valid[winner];target=selected['translation']
                pack=retriever.select_context(pool,tuple(selected['selected_ids']))
                if pack.status!='ready' or pack.context_hash!=selected['context_hash']:
                    raise ValueError('Winning ContextPack changed on resume')
                job.update(winning_attempt=selected['attempt_id'],winning_scomet=scoring['scores'][winner],
                    winning_context=asdict(pack),memory_selection_policy='highest_scomet_once')
            else:
                pack=retriever.build_context(pool)
                if pack.status!='ready':raise ValueError('warmup_context_'+pack.status)
                prompt=(memory.config.prompt_dir/'translate_game.txt').read_text().format(
                    source=source.source,source_lang=source.source_lang,target_lang=source.target_lang,
                    contexts=pack.model_context,source_context=pack.manifest['current_source_context'])
                job.update(warmup_prompt=prompt,warmup_context=asdict(pack));self.checkpoints.save(job)
                target=call('translate_game',prompt).text.strip()
            if not target or translation_issues(source,target,pack,session):raise ValueError('memory_translation_checks_failed')
            self.checkpoints.save(job)
            with self.memory_write_locks.get(job['game'],self.lock):
                existing=[x for x in store.visible(store.view(job['run_id'])) if x.kind=='translation' and x.evidence[0].occurrence_id==source.occurrence_id]
                if existing:
                    saved=existing[0]
                    if saved.content['target']!=target:raise ValueError('Existing memory differs from selected winner')
                else:
                    check=TranslationCheck(source.occurrence_id,source.source_version,text_hash(target),pack.context_hash,
                                           'program:dpo-collection-unreviewed',False)
                    saved=retriever.commit(source,target,check,pack,allow_unreviewed=True)
            job['memory_translation_ref']=asdict(saved.ref);job.setdefault('memory_updates_done',[]);self.checkpoints.save(job)
            if fixed:
                job.update(status='complete',memory_status='translation_recorded_fixed_snapshot',snapshot_stage=True)
                return None
            for kind in ('term','summary','style'):
                if kind in job['memory_updates_done']:continue
                try:
                    drafts=memory.extract_terms(saved.ref) if kind=='term' else retriever.update_memory(source,store.view(job['run_id']),'graph',kind=kind)
                    for draft in drafts:store.approve(draft.ref,'program:unreviewed-collection',verification='unreviewed')
                    job['memory_updates_done'].append(kind);self.checkpoints.save(job)
                except (BudgetExceeded,UncertainCall):raise
                except Exception as error:job['issues'].append(kind+': '+str(error))
            job['memory_status']='updated_unreviewed' if len(job['memory_updates_done'])==3 else 'partial_unreviewed'
            job['status']='complete'
        except BudgetExceeded:
            self.stop.set();return 'budget_exhausted'
        except UncertainCall as error:
            job['issues'].append(str(error));return 'needs_attention_uncertain_call'
        except Exception as error:
            job.update(status='failed',memory_status='failed');job['issues'].append(type(error).__name__+': '+str(error))
        finally:
            job['elapsed_seconds']=job.get('elapsed_seconds',0)+time.monotonic()-started
            if job['status'] in {'complete','failed'}:job['completed_at_unix']=time.time()
            self.checkpoints.save(job)
        return None


def export_states(folder, output):
    state=load_state(folder)
    jobs=[j for j in state['jobs'] if j['phase']=='target' and 'observation' in j and 'state_hash' in j]
    Path(output).write_text(''.join(json.dumps(j,ensure_ascii=False)+'\n' for j in jobs))
    return len(jobs)
