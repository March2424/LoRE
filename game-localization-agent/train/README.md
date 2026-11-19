# 选择器训练

训练环境使用 Linux、单张支持 BF16 的 GPU 及 [固定依赖](../configs/requirements-training.txt)。正式实验使用 RTX PRO 6000 96 GB。首先完成[采样](../sample/README.md)，或者提供已获授权的冻结训练文件。

| 阶段 | 实际数据及训练 | 资产 |
| --- | --- | --- |
| SFT | 3,165 条，LoRA r16，2 epoch、396 步 | [冻结配方](recipes/sft.yaml) · [数据哈希](records/sft/selector_sft_train.metadata.json) · [训练记录](records/sft-train-results.json) |
| DPO | 1,983 对，LoRA r8，beta 0.15，1 epoch、124 步 | [冻结配方](recipes/dpo.yaml) · [数据哈希](records/sha256.json) · [训练记录](records/dpo-train-results.json) |

训练目录须有 LLaMA-Factory 的 `dataset_info.json` 与 `selector_sft_train.json` 或 `selector_dpo_train.json`。DPO 目录还须有与合并基座对应的 `summary.json`。训练文件及模型权重由复现者提供。若重采样，使用新导出的清单与适配器，分数不应标成原实验。

```bash
export PYTHONPATH=.:src:scripts
python3 -m train.configure sft --model "$QWEN_8B" --dataset outputs/mixed-sft \
  --run-dir outputs/sft --output outputs/sft.yaml
python3 scripts/run_selector_sft.py --config outputs/sft.yaml --offline

python3 scripts/merge_selector_sft_for_dpo.py \
  --collection-config outputs/dpo-collect/config.json \
  --preference-summary outputs/dpo-dataset/summary.json --output outputs/sft-merged

python3 -m train.configure dpo --model outputs/sft-merged --dataset outputs/dpo-dataset \
  --run-dir outputs/dpo --output outputs/dpo.yaml
python3 scripts/run_selector_dpo.py --config outputs/dpo.yaml --probe longest
python3 scripts/run_selector_dpo.py --config outputs/dpo.yaml --train
```

合并入口校验采样使用的 SFT 基座与 adapter 指纹，输出 `dpo_reference.json`。选择器加载合并后的混合领域 SFT E2 以及 DPO LoRA。DPO 正式 runner 使用 SwanLab 在线跟踪，基座和数据保持本地文件路径。训练中断后用同一配置加 `--resume`。

原实验可在原服务器的 `outputs/selector_training/{multidomain_2026-11-03,sft_multidomain_96gb,dpo_final_20261107,dpo_pro6000_20261107}` 获取训练表、SFT adapter 和 DPO adapter。合并基座原始位置为 `/root/dpo-runtime/pi_sft_multidomain_e2_merged`。DPO adapter SHA-256 为 `21c3d89a8b71419fac1e155020c8517893bb057739dc9bb03231f4e29d916ad2`。访问这些资产需要原项目授权。
