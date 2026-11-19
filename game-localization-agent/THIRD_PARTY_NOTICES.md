# 第三方来源与许可

## DocMTAgent / DelTA

- 作者仓库：[YutongWang1216/DocMTAgent](https://github.com/YutongWang1216/DocMTAgent)。
- 核验日期：2026-10-28。
- 固定提交：`6dde70acc9e60fd2533c2ca467eb327884d20dab`。
- 论文：[DelTA: An Online Document-Level Translation Agent Based on Multi-Level Memory，ICLR 2025](https://openreview.net/forum?id=hoYFLRNbhc)。
- 上游许可证：Apache License 2.0，完整副本见 [随附许可证](third_party/DocMTAgent/LICENSE)。该提交未发现单独的 NOTICE 文件。

### DelTA 对照运行

`third_party/DocMTAgent/` 附带固定提交的 `infer/infer_qwen.py`、两个语言方向的提示词和检索提示词。[delta_runtime.py](scripts/delta_runtime.py) 直接加载上游四个记忆类，注入本地 Qwen 和受限的候选 ID 解析。正式推理和离线测试直接读取随附文件。历史 `run_delta_eval.py prepare` 若需校验上游 Git 提交，请将配置中的 `upstream` 指向该提交的完整 Git 检出。

## Qwen2.5-7B-Instruct budget tokenizer

`data/graphs/tokenizer.json` is from [Qwen/Qwen2.5-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct) at revision `a09a35458c702b33eeacc393d103063234e8bc28`. This is a tokenizer asset used for context budgeting. See the [Apache 2.0 license text](third_party/DocMTAgent/LICENSE) and the [original tokenizer provenance](data/graphs/tokenizer-provenance.json).
