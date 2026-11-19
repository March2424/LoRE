"""Profile or train the frozen selector preference dataset on a single GPU."""

import argparse
import fcntl
import importlib.metadata
import json
import shutil
import signal
import uuid
from pathlib import Path

import torch
import torch.nn.functional as F
import yaml
from llamafactory.train.dpo import trainer as dpo_trainer
from llamafactory.train.tuner import run_exp
from transformers.trainer_pt_utils import LengthGroupedSampler

from run_selector_sft import RunState, complete_checkpoints, digest, write_json


RUN_DIR = Path('artifacts/dpo-pro6000-20261107')
DEFAULT_CONFIG = Path('configs/training/qwen3_8b_dpo_pro6000.yaml')


class SelectorDPOTrainer(dpo_trainer.CustomDPOTrainer):
    """Keep the native DPO objective and measure KL on two completion prefixes per side."""

    def _get_train_sampler(self, train_dataset=None):
        if self.args.train_sampling_strategy != 'group_by_length':
            return super()._get_train_sampler(train_dataset)
        dataset = self.train_dataset if train_dataset is None else train_dataset
        lengths = [max(len(chosen), len(rejected)) for chosen, rejected in
                   zip(dataset['chosen_input_ids'], dataset['rejected_input_ids'])]
        return LengthGroupedSampler(self.args.train_batch_size * self.args.gradient_accumulation_steps,
                                    lengths=lengths)

    def concatenated_forward(self, model, batch, is_ref_model=False):
        result = super().concatenated_forward(model, batch, is_ref_model)
        # Logit position i predicts label i+1. Sample the first and middle supervised positions.
        with torch.no_grad():
            mask = batch['labels'][:, 1:].ne(-100).int()
            first = mask.argmax(dim=-1)
            last = mask.shape[1] - 1 - mask.flip(dims=(-1,)).argmax(dim=-1)
            positions = torch.stack((first, (first + last) // 2), dim=1)
            positions = positions.chunk(2)
            rows = torch.arange(positions[0].shape[0], device=mask.device)[:, None]
            log_probs = torch.stack([
                F.log_softmax(result[f'{side}_logits'][rows, indices], dim=-1)
                for side, indices in zip(('chosen', 'rejected'), positions)
            ])
            if is_ref_model:
                kl = F.kl_div(log_probs, self.policy_prefix_log_probs, log_target=True, reduction='none')
                self.prefix_kl = kl.sum(dim=-1).mean(dim=(1, 2)).clamp_min(0).tolist()
                del self.policy_prefix_log_probs
            else:
                self.policy_prefix_log_probs = log_probs
        return result

    def get_batch_loss_metrics(self, model, batch, train_eval='train'):
        loss, metrics = super().get_batch_loss_metrics(model, batch, train_eval)
        prefix = 'eval_' if train_eval == 'eval' else ''
        chosen, rejected = self.prefix_kl
        metrics[f'{prefix}kl/conditional_chosen'] = chosen
        metrics[f'{prefix}kl/conditional_rejected'] = rejected
        metrics[f'{prefix}kl/conditional_mean'] = (chosen + rejected) / 2
        if train_eval == 'train':
            attention = batch['attention_mask']
            self.run_state.pairs += attention.shape[0] // 2
            self.run_state.tokens += int(attention.sum().item())
            self.run_state.padded_tokens += attention.numel()
        return loss, metrics


class DPORunState(RunState):
    def on_step_begin(self, args, state, control, **kwargs):
        self.pairs = self.tokens = self.padded_tokens = 0
        return super().on_step_begin(args, state, control, **kwargs)

    def on_log(self, args, state, control, logs=None, **kwargs):
        logs = dict(logs or {})
        if 'loss' in logs:
            logs.update({
                'rewards/mean': (logs['rewards/chosen'] + logs['rewards/rejected']) / 2,
                'throughput/pairs_per_second': self.pairs / self.steps[-1],
                'throughput/tokens_per_second': self.tokens / self.steps[-1],
                'throughput/padding_fraction': 1 - self.tokens / self.padded_tokens,
                'gpu/peak_allocated_gib': torch.cuda.max_memory_allocated() / 1024**3,
                'gpu/peak_reserved_gib': torch.cuda.max_memory_reserved() / 1024**3,
            })
        return super().on_log(args, state, control, logs=logs, **kwargs)


def prepare_probe_data(source, data, name):
    filename = 'selector_dpo_train.json'
    pairs = json.loads((source / filename).read_text())
    audit = json.loads((source / 'audit.json').read_text())
    ordered = sorted((row for row in audit if row['status'] == 'pair'),
                     key=lambda row: max(row['sequence_tokens'].values()), reverse=True)
    sample = ordered[:32] if name == 'longest' else [ordered[i * len(ordered) // 32] for i in range(32)]
    data.mkdir(parents=True, exist_ok=True)
    write_json(data / filename, [pairs[row['pair_index']] for row in sample])
    shutil.copyfile(source / 'dataset_info.json', data / 'dataset_info.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--probe', choices=('longest', 'typical'))
    mode.add_argument('--train', action='store_true')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--micro-batch', type=int, choices=(1, 2, 4, 8, 16))
    parser.add_argument('--no-checkpointing', action='store_true')
    parser.add_argument('--stop-after', type=int)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not args.probe and (args.micro_batch or args.no_checkpointing):
        parser.error('Freeze measured batch settings in the formal YAML')
    if torch.cuda.device_count() != 1 or not torch.cuda.is_bf16_supported():
        raise RuntimeError('Expose one BF16 CUDA GPU to this run')

    config = yaml.safe_load(args.config.read_text())
    source = Path(config['dataset_dir'])
    model = Path(config['model_name_or_path'])
    reference_manifest = model / 'dpo_reference.json'
    reference = json.loads(reference_manifest.read_text())
    summary = json.loads((source / 'summary.json').read_text())
    if reference['selector_policy_sha256'] != summary['selector_policy_sha256']:
        raise ValueError('The merged reference and preference data have different SFT policy IDs')
    if args.probe:
        micro_batch = args.micro_batch or 1
        checkpointing = not args.no_checkpointing
        probe = RUN_DIR / 'profiles' / f'{args.probe}-mb{micro_batch}-gc{int(checkpointing)}'
        config.update(dataset_dir=str(probe / 'data'), output_dir=str(probe), max_steps=2,
                      per_device_train_batch_size=micro_batch, gradient_accumulation_steps=16 // micro_batch,
                      gradient_checkpointing=checkpointing, disable_gradient_checkpointing=not checkpointing,
                      dataloader_num_workers=0)
    output = Path(config['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output).free < 5 * 1024**3:
        raise OSError('Keep at least 5 GiB free on the checkpoint volume')

    with (output / '.training.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checkpoints = complete_checkpoints(output)
        if args.resume and not checkpoints:
            raise RuntimeError('No complete resumable checkpoint found')
        if not args.resume and (checkpoints or (output / 'metrics.jsonl').exists()):
            raise RuntimeError('Run already has training history; use --resume or a new output directory')
        resume = checkpoints[-1] if args.resume else None
        data = Path(config['dataset_dir'])
        if args.probe and not resume:
            prepare_probe_data(source, data, args.probe)
        fingerprint = {
            'config': config,
            'dataset_sha256': digest(data / 'selector_dpo_train.json'),
            'dataset_info_sha256': digest(data / 'dataset_info.json'),
            'reference_manifest_sha256': digest(reference_manifest),
            'source_policy_sha256': reference['selector_policy_sha256'],
            'runner_sha256': digest(__file__),
            'versions': {p: importlib.metadata.version(p) for p in
                         ('torch', 'transformers', 'llamafactory', 'trl', 'peft', 'accelerate', 'swanlab')},
            'gpu': torch.cuda.get_device_name(0),
        }
        meta_path = output / 'run.json'
        metadata = json.loads(meta_path.read_text()) if meta_path.exists() else {'fingerprint': fingerprint}
        if metadata['fingerprint'] != fingerprint:
            raise ValueError('Frozen DPO config, data, model manifest or environment changed')
        tracker = None
        if args.train:
            import swanlab

            metadata.setdefault('swanlab_run_id', uuid.uuid4().hex[:21])
            write_json(meta_path, metadata)
            tracker = swanlab.init(project='LLM_Project', id=metadata['swanlab_run_id'],
                                   name=config['run_name'], mode='online', public=False,
                                   log_dir=str(output / 'swanlog'), config=fingerprint,
                                   resume='must' if metadata.get('swanlab_url') else 'allow')
            metadata['swanlab_url'] = tracker.url
        write_json(meta_path, metadata)
        if resume:
            config = {**config, 'resume_from_checkpoint': str(resume)}
        callback = DPORunState(output, resume, args.stop_after, tracker)
        signal.signal(signal.SIGTERM, callback.request_stop)
        signal.signal(signal.SIGINT, callback.request_stop)
        SelectorDPOTrainer.run_state = callback
        # LLaMA-Factory resolves this class inside its DPO workflow at runtime.
        dpo_trainer.CustomDPOTrainer = SelectorDPOTrainer
        try:
            run_exp(config, callbacks=[callback])
            progress_path = output / 'progress.json'
            progress = json.loads(progress_path.read_text())
            complete = progress['global_step'] >= progress['max_steps']
            write_json(progress_path, {**progress, 'status': 'complete' if complete else 'stopped'})
        except BaseException as error:
            write_json(output / 'failure.json', {
                'type': type(error).__name__,
                'message': str(error),
                'peak_allocated_bytes': torch.cuda.max_memory_allocated(),
                'peak_reserved_bytes': torch.cuda.max_memory_reserved(),
            })
            if tracker:
                tracker.finish(state='crashed', error=str(error))
            raise
        if tracker:
            tracker.finish(state='success' if complete else 'aborted')


if __name__ == '__main__':
    main()
