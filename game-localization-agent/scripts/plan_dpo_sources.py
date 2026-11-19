"""Select disjoint train-only DPO targets and warmups; no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
from localization_agent.selection.preferences import plan_sources


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--game-root',type=Path,required=True)
    p.add_argument('--public-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--seed',type=int,default=20261103)
    args=p.parse_args()
    if args.output.exists(): raise ValueError('Use a new output path')
    plan=plan_sources(args.game_root,args.public_root,seed=args.seed)
    files=[args.game_root/'sources.jsonl',args.game_root/'splits.jsonl',args.game_root/'offline/references.jsonl',
           args.public_root/'sources_manifest.jsonl',args.public_root/'offline/references.jsonl']
    plan['input_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':plan['status'],'actual_targets':plan['actual_targets'],'warmup_projects':len(plan['warmup_ids'])},ensure_ascii=False))


if __name__=='__main__': main()
