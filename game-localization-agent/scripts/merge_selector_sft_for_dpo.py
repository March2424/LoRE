"""Merge the frozen SFT E2 adapter into a standalone BF16 DPO reference."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--collection-config', type=Path,
                        default=Path('outputs/dpo_context_full_capped_20261106/config.json'))
    parser.add_argument('--preference-summary', type=Path,
                        default=Path('outputs/selector_training/dpo_final_20261107/summary.json'))
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    collected = json.loads(args.collection_config.read_text())
    summary = json.loads(args.preference_summary.read_text())
    fingerprints = collected['policy_files']
    policy = hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest()
    if policy != summary['selector_policy_sha256']:
        raise ValueError('Collected policy differs from the final preference data')
    for path, expected in fingerprints.items():
        if digest(path) != expected:
            raise ValueError(f'Frozen SFT policy file changed: {path}')

    base = Path(collected['policy_base'])
    adapter = Path(collected['policy_adapter'])
    model_bytes = sum(path.stat().st_size for path in base.glob('*.safetensors'))
    reserve = 10 * 1024**3
    if shutil.disk_usage(output.parent).free < model_bytes + reserve + 256 * 1024**2:
        raise OSError('Merge requires enough space for BF16 weights and 10 GiB of free space')

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    torch.set_num_threads(min(8, len(os.sched_getaffinity(0))))
    print(f'Loading frozen SFT policy {policy} on CPU', flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        base, dtype=torch.bfloat16, device_map={'': 'cpu'}, local_files_only=True)
    model = PeftModel.from_pretrained(model, adapter).merge_and_unload(safe_merge=True)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    with tempfile.TemporaryDirectory(prefix='.sft-merge-', dir=output.parent) as temp:
        staged = Path(temp) / 'model'
        model.save_pretrained(staged, max_shard_size='4GB')
        tokenizer.save_pretrained(staged)
        if shutil.disk_usage(output.parent).free < reserve:
            raise OSError('Merge would leave less than 10 GiB of free space')
        files = {path.name: digest(path) for path in sorted(staged.iterdir()) if path.is_file()}
        (staged / 'dpo_reference.json').write_text(json.dumps({
            'selector_policy_sha256': policy,
            'dtype': 'bfloat16',
            'files': files,
        }, indent=2) + '\n')
        staged.rename(output)
    print(f'Exported merged SFT reference to {output}', flush=True)


if __name__ == '__main__':
    main()
