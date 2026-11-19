import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localization_agent.memory import MemoryStore
from localization_agent.models.protocols import ModelReply
from localization_agent.selection.checkpoints import load_state, write_json
from localization_agent.selection.dpo_collector import DPOCollector
from test_teacher_runtime import fixture_folder


class Selector:
    def __init__(self): self.calls=0
    def sample(self,prompt,seed):
        obs=json.loads(prompt.split('Observation: ',1)[1]);self.calls+=1
        ids=[] if self.calls==1 else ([obs['candidates'][0]['id']] if obs['candidates'] else [])
        return json.dumps({'rationale':'Choose available evidence as appropriate.','selected_ids':ids}), 'stop'


class Model:
    def __init__(self,budget,**kwargs):self.budget=budget
    def __call__(self,task,prompt):
        self.budget.reserve(1,0.000001)
        if task=='translate_game':
            import hashlib
            return ModelReply('translation '+hashlib.sha256(prompt.encode()).hexdigest()[:8],'fixture')
        if task=='extract_terms':return ModelReply('[]','fixture')
        return ModelReply('fixture note','fixture')


class Scorer:
    identity={'sha256':'fixture'}
    def score(self,project,source_id,source,translations):
        assert len(translations)==2
        return [0.1,0.9],{'sha256':'fixture'},'train'


class DPOCollectorTests(unittest.TestCase):
    @patch('localization_agent.selection.collector.LocalTokenCounter',return_value=len)
    def test_highest_scored_translation_is_only_memory_writeback_and_resume_is_idempotent(self,_):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'run';fixture_folder(folder,games=('flare',),targets=1)
            config=json.loads((folder/'config.json').read_text())
            config['policy_sha256']='fixture-policy';config['graph'].update(max_soft_tokens=2048,max_characters=12000)
            write_json(folder/'config.json',config)
            selector=Selector();result=DPOCollector(folder,config,{},selector,Scorer(),Model).run()
            jobs=load_state(folder)['jobs'];job=jobs[1]
            self.assertEqual(job['status'],'complete',job.get('issues'))
            self.assertEqual(job['winning_attempt'],'1')
            self.assertEqual(job['winning_scomet'],0.9)
            self.assertEqual([a['status'] for a in job['attempts']],['complete','complete','invalid','invalid'])
            store=MemoryStore(folder/'flare.sqlite')
            try:
                visible=store.visible(store.view(job['run_id']))
                targets=[x for x in visible if x.kind=='translation' and x.evidence[0].occurrence_id==job['source_id']]
                self.assertEqual(len(targets),1)
                self.assertEqual(targets[0].content['target'],job['attempts'][1]['translation'])
            finally:store.close()
            DPOCollector(folder,config,{},selector,Scorer(),Model).run()
            self.assertEqual(selector.calls,4)


if __name__=='__main__': unittest.main()
