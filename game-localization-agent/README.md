# 基于图谱和翻译记忆的游戏本地化 Agent

系统按资源位置读取游戏文本，从源侧实体关系图和当前运行的记忆中检索证据，由选择器挑选软证据，保留适用硬约束，再交给翻译模型。程序检查通过后，译文和提取出的软记忆可在同一运行内复用。评测单独读取对齐的参考译文。选择器采用 Qwen3-8B，经 Teacher SFT 和离线 DPO 训练。

```text
data/         游戏数据适配产物、冻结测试集、示例与源侧图谱
src/          图谱、记忆、检索、上下文选择和模型适配
configs/      各阶段配置与提取协议
prompts/      建图、记忆、选择、翻译和评审提示词
sample/       Teacher 与 DPO 轨迹采集说明和准备工具
train/        冻结 SFT/DPO 配方
inference/    小样本、独立翻译与正式推理入口
evaluate/     评分输入、校验及复评入口
results/      上传译文的结果
scripts/      复用的导入、提取、训练、翻译和评分脚本
tests/        离线测试
installation/ 各实验环境的依赖
```

目录组织参考 [LoongDocMT](https://github.com/YutongWang1216/LoongDocMT)。DelTA 对照使用固定提交中的记忆组件，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 1. 从 CSV 跑通整条链路

在本目录执行。快速验证使用 Python 3.11+，无需安装模型。正式训练和离线 Doc-COMET 使用独立环境。

```bash
export PYTHONPATH=.:src:scripts
python3 -m inference.smoke --output outputs/smoke-offline
python3 -m evaluate.verify
```

小样本依次运行 CSV 导入、参考拆分、实体关系提取、作用域检索、选择、占位符检查、运行内写回及离线对齐。检查两条源文的翻译记录，以及第二条能否召回第一条产生的记忆。输出包括图谱 SQLite、译文 JSONL、实际输入清单、记忆引用及 `smoke.json`。

可在环境中设置 `DEEPSEEK_API_KEY`，用相同的小样本验证真实 API。

```bash
python3 -m inference.smoke --backend deepseek --output outputs/smoke-deepseek
```

## 2. 使用自己的数据

输入为 UTF-8、四列 `id,source_text,text_type,context` 的源侧 CSV。每个使用位置具有独立 ID，原生条件保留在 `context`。测试参考分开保存。三款游戏的样本、原始快照入口、字段和数据许可见 [数据说明](data/README.md)。

以包内的两条资源为例，建图与推理可分开运行。命令需要环境变量 `DEEPSEEK_API_KEY`。

```bash
python3 scripts/run_benchmark_agent.py data/demo/benchmark.csv \
  --project demo --config configs/submission.smoke.json \
  --db outputs/demo/graph.sqlite --output outputs/demo/extraction.json

python3 -m pip install -r configs/requirements-retrieval.txt
python3 -m inference.translate --project demo --graph outputs/demo/graph.sqlite \
  --config configs/submission.inference.json --tokenizer data/graphs/tokenizer.json \
  --output outputs/demo/translation
```

`--ids` 可指定待译 ID 单，顺序即执行顺序。翻译、选择、记忆服务可在推理配置中分别指定。测试参考仅在后续评分阶段读取。经过程序检查的自动译文标记为 `unreviewed`，存于独立运行记忆。大规模建图可用游戏配置与 `--dry-run` 先核对批数和调用预算。

## 3. 正式实验与测试

- [实验口径](docs/EXPERIMENT.md)说明各条件的推理入口、模型和数据边界。
- [采样](sample/README.md)说明如何准备训练数据和导出轨迹。[训练配方](train/recipes/)保留 SFT 和 DPO 的实验参数。
- [评测复现](evaluate/README.md)列出 sCOMET、Doc-COMET、BLEU-4 和 LTCR 的运行环境。测试参考、冻结译文和结果记录均按 occurrence ID 及文档行对齐。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r installation/requirements-test.txt
python -m unittest discover -s tests
```

全套原始资源适配测试另用 GNU gettext 和可选上游快照。详见 [数据说明](data/README.md)。

## 4. 已提交结果

`results/` 保存各组评测结果。结果按 `数据集/模型/方法/语言方向/目标语言.文档ID` 排列。每组同时提供逐条 `records.json`、指标 `scores.json` 及对齐的语言文件。

[评分汇总](evaluate/summary.json)包含通用领域 24 组、Naev 100 条多模型 9 组、Naev 300 条消融 5 组，共 4,788 条译文记录。

本包已跑通离线和 DeepSeek 小样本流程，现有测试发现 100 项，91 项通过，9 项因缺少可选原始快照或历史采样产物跳过。Naev 两套实验的 14 组 BLEU-4 已重算并与冻结分数完全一致。正式训练、GPU 翻译与其余指标的全量重跑需要相应环境和外部资产。本次提交附带它们的输入、评分文件及代码入口。
