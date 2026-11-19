# 推理复现

从提交根目录执行，设置 `PYTHONPATH=.:src:scripts`。面向自定义 CSV 和 DeepSeek 的小样本命令见[主 README](../README.md)。

正式对照使用冻结的 occurrence ID、源图、选择器、Qwen 翻译后端与提示词。准备步骤在本机解包并校验源侧图，不调用模型。

```bash
python3 -m inference.formal prepare --output outputs/formal
```

正式运行使用带 BF16 支持的 CUDA 环境和 [训练依赖](../configs/requirements-training.txt)。环境变量指向本地模型目录。

| 变量 | 内容 |
| --- | --- |
| `QWEN_7B` | Qwen2.5-7B-Instruct |
| `QWEN_8B` | Qwen3-8B |
| `QWEN_14B` | Qwen3-14B |
| `SELECTOR_SFT` | 已合并的混合领域 SFT E2 基座 |
| `SELECTOR_DPO` | DPO LoRA adapter 目录 |
| `LLM_API_KEY` | 正式框架记忆更新所需的 DeepSeek 密钥 |
| `BUDGET_TOKENIZER` | 可选 tokenizer 路径，默认使用包内文件 |

模型及 adapter 的冻结指纹见 [通用实验配置](../evaluate/frozen/general/experiment_config.json)，训练产物说明见 [训练指南](../train/README.md)。Doc2Doc 同时读取三套模型的 tokenizer 以计算共同的文档窗口。Naev 每个记忆条件使用独立 SQLite，允许调用前逐组确认数据、设备和预算。

```bash
python3 -m inference.formal run --work outputs/formal --suite general --condition 8b/news/framework
python3 -m inference.formal run --work outputs/formal --suite naev100 --condition 14b_delta
python3 -m inference.formal run --work outputs/formal --suite naev300 --condition base_selector
```

完整 38 组运行编号见各组清单。[通用](../evaluate/frozen/general/provenance.json) · [Naev 100](../evaluate/frozen/naev100/provenance.json) · [Naev 300](../evaluate/frozen/naev300/provenance.json)

每个条件在 `outputs/formal/runs/<suite>/<condition>/` 下保留检查点和面向评分的 `state.json`。失败样本保留原 ID。正式方法的模型角色、候选预算和处理顺序见 [复现口径](../docs/EXPERIMENT.md)。生成结果可用 `evaluate.reproduce prepare --generated outputs/formal` 送入原评分协议。提供的 `results/` 对应上传修订稿，重跑生成流程得到自动译文。
