# 采样与训练数据构建

在提交根目录运行，设置 `PYTHONPATH=.:src:scripts`。采样前准备预算 tokenizer 和源侧数据库，运行采样时使用环境变量 `LLM_API_KEY`。

## Teacher 轨迹与 SFT 数据

```bash
python3 -m sample.bootstrap
python3 scripts/prepare_training_data.py --output outputs/game-plan --memory-coverage
python3 scripts/generate_teacher_trajectories.py prepare \
  --plan outputs/game-plan --output outputs/game-teacher --max-cost 2
python3 scripts/generate_teacher_trajectories.py run --output outputs/game-teacher
python3 scripts/generate_teacher_trajectories.py export --output outputs/game-teacher \
  --dataset-dir outputs/game-sft
```

上述命令按游戏资源组生成独立 train 流，先预热记忆，再为目标资源采集 Teacher 的理由与候选 ID。配置中的 2 USD 是这个例子的估算预算上限，按本轮数据量检查并填写。实际混合 SFT 使用 1,966 条已有游戏轨迹与 1,199 条新增通用语料轨迹，完整历史轨迹由复现者从授权输入取得。重新调用外部模型可能生成不同的选择动作。

通用语料使用 [LoongDocMT](https://github.com/YutongWang1216/LoongDocMT) 的 `data/news_commentary_v18.1` 和 [DocMTAgent](https://github.com/YutongWang1216/DocMTAgent) 的 `data/guofeng_v1/TEST_2`。从上游取得后设置 `NEWS_DATA` 和 `GUOFENG_DATA` 为这两个目录。翻译评测的四个冻结文件已经随 [data](../data/README.md) 提供。

```bash
PUBLIC=outputs/multidomain_sft_2026-11-03
python3 scripts/prepare_multidomain_sources.py --news "$NEWS_DATA" \
  --guofeng "$GUOFENG_DATA" --output "$PUBLIC"
python3 scripts/run_multidomain_collection.py --sources "$PUBLIC" --dry-run
python3 scripts/run_multidomain_collection.py --sources "$PUBLIC" \
  --stage graph --execute --continue-after-pilot
python3 scripts/align_multidomain_graphs.py --sources "$PUBLIC"
python3 scripts/prepare_multidomain_teacher.py --sources "$PUBLIC" --output outputs/public-plan
python3 scripts/run_multidomain_teacher.py --plan outputs/public-plan \
  --old-dataset outputs/game-sft/selector_sft_train.json \
  --output outputs/public-teacher --dataset-dir outputs/mixed-sft
```

`--prepare-only` 可先生成并检查混合采样计划。上游文档 ID 和方向参与分组，News 留出文件排除在训练和奖励外，GuoFeng TEST_2 在此作为 seen 适配数据。训练只监督选择动作，翻译与记忆更新用于推进后续观察。

## DPO 偏好数据

先完成混合 SFT。设置 `QWEN_8B` 为原始基座目录，`SELECTOR_SFT_ADAPTER` 为 SFT E2 adapter。如果使用捕获的正式训练输入，保存对应的来源及校验值。全量采样按阶段指定设备与预算。

```bash
GAME=outputs/cpu_preparation_2026-11-02_2000_context_v2
PUBLIC=outputs/multidomain_sft_2026-11-03
python3 scripts/prepare_training_data.py --output "$GAME" --memory-coverage
python3 -m sample.bootstrap --public-root "$PUBLIC" --teacher-plan outputs/public-plan
python3 scripts/plan_dpo_sources.py --game-root "$GAME" --public-root "$PUBLIC" \
  --output outputs/dpo-sources.json
python3 scripts/run_dpo_collection.py prepare --source-plan outputs/dpo-sources.json \
  --output outputs/dpo-collect --pilot --max-cost 2 --comet-checkpoint "$COMET_CHECKPOINT"
python3 scripts/run_dpo_collection.py run --output outputs/dpo-collect \
  --comet-python "$COMET_PYTHON" --comet-gpus 0
python3 scripts/finalize_dpo_collection.py --stage outputs/dpo-collect \
  --tokenizer "$QWEN_8B" --output outputs/dpo-dataset
```

同一冻结观察采样多种选择动作，固定翻译服务各自生成译文，隔离的句级 COMET 进程在训练参考上评分。最高与最低分构成严格正分差的偏好对。终稿收集可重复传入互不重叠的 `--stage`。历史生效条件、数据指纹与统计留在 [DPO 数据清单](../train/records/summary.json)。
