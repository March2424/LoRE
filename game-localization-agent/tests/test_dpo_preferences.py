import json
import unittest
import tempfile
from pathlib import Path
from localization_agent.selection.preferences import allowed_sources, build_preferences, digest, CometLengthError


class Scorer:
    identity={'sha256':'fixture'}
    def score(self, rows):
        return [0.8 if row['mt']=='better' else 0.7 for row in rows]


def state(project, source_id):
    obs={'source':'source','source_id':source_id,'pool_hash':'pool','soft_token_budget':2048,'max_selected':8,
         'candidates':[{'id':'c01'},{'id':'c02'}]}
    prompt=Path('prompts/selection/context_selector.txt').read_text().format(observation=json.dumps(obs,ensure_ascii=False))
    attempts=[]
    for i, (cid, translation) in enumerate([('c01','better'),('c02','worse')]):
        completion=json.dumps({'rationale':'reason','selected_ids':[cid]})
        attempts.append({'attempt_id':str(i),'completion':completion,'translation':translation,'finish_reason':'stop',
            'pool_hash':'pool','policy_sha256':'policy','context_hash':'ctx'+str(i),
            'validation':{'status':'passed','checks':{'action_valid':True,'context_budget_passed':True,'translation_checks_passed':True},
                'state_hash':'state','source_sha256':digest('source'),'pool_hash':'pool','context_hash':'ctx'+str(i),
                'action_sha256':digest(completion),'translation_sha256':digest(translation),
                'max_soft_tokens':2048,'max_characters':12000}})
    return {'project_id':project,'source_id':source_id,'pool_hash':'pool','state_hash':'state',
            'policy_sha256':'policy','instruction':prompt,'observation':obs,'attempts':attempts,
            'graph_config':{'max_soft_tokens':2048,'max_characters':12000,'max_selected':8,'max_candidates':16}}


class PreferenceTests(unittest.TestCase):
    def test_selects_by_segment_score_and_never_exports_reference(self):
        a=state('flare','a');refs={('flare','a'):{'source':'source','reference':'private-reference','group':'game'}}
        pairs,audit,counts=build_preferences([a],refs,Scorer())
        self.assertEqual(counts['pairs'],1)
        self.assertEqual(json.loads(pairs[0]['chosen'])['selected_ids'],['c01'])
        self.assertNotIn('private-reference',json.dumps(pairs))
        self.assertAlmostEqual(audit[0]['score_delta'],0.1)

    def test_rejects_stale_checks_and_forbidden_source(self):
        a=state('flare','a');a['attempts'][0]['translation']='changed'
        forbidden=state('test','b');refs={('flare','a'):{'source':'source','reference':'r','group':'game'}}
        pairs,audit,_=build_preferences([a,forbidden],refs,Scorer())
        self.assertEqual(pairs,[])
        self.assertEqual(audit[0]['attempts'][0]['status'],'missing_or_stale_validation')
        self.assertEqual(audit[1]['status'],'outside_allowed_train_or_missing_reference')

    def test_game_mix_cap_and_policy_consistency(self):
        states=[state('flare','g'),state('news','n1'),state('news','n2')]
        refs={(s['project_id'],s['source_id']):{'source':'source','reference':'r','group':'game' if s['project_id']=='flare' else 'news_en_zh'} for s in states}
        pairs,audit,counts=build_preferences(states,refs,Scorer())
        self.assertEqual((len(pairs),counts['retained_game_pairs'],counts['retained_news_pairs']),(1,1,0))
        states[-1]['policy_sha256']='different'
        with self.assertRaisesRegex(ValueError,'Mixed'):build_preferences(states,refs,Scorer())

    def test_guofeng_seen_is_explicitly_allowed_but_test_news_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);game=root/'game';public=root/'public'
            for folder in (game/'offline', public/'offline'):folder.mkdir(parents=True)
            def write(path,rows):path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
            write(game/'sources.jsonl',[]);write(game/'splits.jsonl',[]);write(game/'offline/references.jsonl',[])
            guo={'project_id':'guo','source_id':'g','sft_allowed':True,'split':'adaptation_seen',
                 'corpus':'guofeng_v1_test2','source_lang':'zh','target_lang':'en','source_hash':digest('source')}
            news={**guo,'project_id':'news','source_id':'n','split':'test','corpus':'news_commentary_v18.1'}
            write(public/'sources_manifest.jsonl',[guo,news])
            refs=[{'project_id':x['project_id'],'source_id':x['source_id'],'split':x['split'],
                   'source_lang':'zh','target_lang':'en','reference_text':'reference','dpo_reference_allowed':False} for x in (guo,news)]
            write(public/'offline/references.jsonl',refs)
            allowed=allowed_sources(game,public)
            self.assertEqual(set(allowed),{('guo','g')})
            self.assertEqual(allowed[('guo','g')]['reference_use'],'user_reclassified_train_seen_2026-11-03')

    def test_uses_the_same_scores_that_selected_memory(self):
        class CachedOnly(Scorer):
            def score(self,rows): raise AssertionError('must reuse recorded scores')
        a=state('flare','a')
        a['scoring']={'scorer':{'sha256':'fixture'},'attempts':[
            {'attempt_id':x['attempt_id'],'translation_sha256':digest(x['translation'])} for x in a['attempts']],
            'scores':[0.8,0.7]}
        refs={('flare','a'):{'source':'source','reference':'r','group':'game'}}
        pairs,_,_=build_preferences([a],refs,CachedOnly())
        self.assertEqual(json.loads(pairs[0]['chosen'])['selected_ids'],['c01'])

    def test_long_scorer_input_recorded(self):
        class Long(Scorer):
            def score(self,rows): raise CometLengthError('too long')
        a=state('flare','a');refs={('flare','a'):{'source':'source','reference':'r','group':'game'}}
        pairs,audit,_=build_preferences([a],refs,Long())
        self.assertEqual(pairs,[]);self.assertEqual(audit[0]['status'],'scorer_input_too_long')


if __name__=='__main__': unittest.main()
