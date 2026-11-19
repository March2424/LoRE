"""Isolated sCOMET process: reference text remains in this process only."""
import argparse
import contextlib
import json
import math
import sys
from pathlib import Path
from localization_agent.selection.preferences import LocalSComet, allowed_sources, digest, CometLengthError


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--game-root',type=Path,required=True);p.add_argument('--public-root',type=Path,required=True)
    p.add_argument('--checkpoint',type=Path,required=True);p.add_argument('--gpus',type=int,choices=(0,1),default=1)
    a=p.parse_args()
    with contextlib.redirect_stdout(sys.stderr):
        refs=allowed_sources(a.game_root,a.public_root);scorer=LocalSComet(a.checkpoint,batch_size=1,gpus=a.gpus)
    sys.stdout.write(json.dumps({'status':'ready','identity':scorer.identity})+'\n');sys.stdout.flush()
    for line in sys.stdin:
        try:
            q=json.loads(line);reference=refs.get((q['project_id'],q['source_id']))
            if not reference:raise ValueError('source_not_allowed_for_reward')
            if digest(q['source'])!=reference.get('source_hash',digest(reference.get('source',''))):raise ValueError('source_hash_mismatch')
            inputs=[{'src':q['source'],'mt':t,'ref':reference['reference']} for t in q['translations']]
            scores=[None]*len(inputs);eligible=[];indices=[]
            for i,item in enumerate(inputs):
                try:scorer.validate_length(item);eligible.append(item);indices.append(i)
                except CometLengthError:pass
            if eligible:
                try:
                    with contextlib.redirect_stdout(sys.stderr):values=scorer.score(eligible)
                except Exception as error:
                    import torch
                    if not isinstance(error,torch.cuda.OutOfMemoryError) or not scorer.gpus:raise
                    print('COMET GPU OOM; retrying scoring on CPU',file=sys.stderr,flush=True)
                    scorer.model.cpu();torch.cuda.empty_cache();scorer.gpus=0
                    with contextlib.redirect_stdout(sys.stderr):values=scorer.score(eligible)
                if len(values)!=len(eligible) or any(not math.isfinite(float(x)) for x in values):raise ValueError('invalid_scores')
                for i,value in zip(indices,values):scores[i]=float(value)
            answer={'status':'ok','scores':scores,'identity':scorer.identity,
                    'reference_use':reference.get('reference_use','train'), 'scoring_device':'cuda' if scorer.gpus else 'cpu'}
        except CometLengthError as error:answer={'status':'too_long','error':str(error)}
        except Exception as error:answer={'status':'error','error':type(error).__name__+': '+str(error)}
        sys.stdout.write(json.dumps(answer,ensure_ascii=False)+'\n');sys.stdout.flush()


if __name__=='__main__':main()
