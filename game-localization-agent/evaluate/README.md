# 评测与结果核验

在提交根目录设置 `PYTHONPATH=.:src:scripts`。包内 [results](../results/README.md) 是 2026-11-17 新版上传译文的重评分。原始使用记录、修改审计、共同比较覆盖和汇总见 [运行记录](run_record.md)、[修订审计](../results/revision.json)及[汇总表](report.md)。

```bash
python3 -m evaluate.verify
shasum -a 256 -c evaluate/experiment/results.sha256
python3 -m evaluate.reproduce prepare --output outputs/rescore
```

第一条校验测试清单、文档顺序、原译文行、冻结评分状态与分数。第二条核对原服务器导出的 209 个结果文件 SHA-256。准备命令复制独立评分工作区；如要评分一套完整的全新正式翻译结果，加 `--generated outputs/formal`。

## BLEU-4

在安装 `sacrebleu==2.6.0` 的环境执行。

```bash
python3 -m evaluate.reproduce metric --work outputs/rescore --suite naev100 --metric bleu
python3 -m evaluate.reproduce metric --work outputs/rescore --suite naev300 --metric bleu
```

按固定资源顺序使用中文分词与一份参考，保持失败条目位置。两套实验的 14 个条件均已在本提交包重算，与保存结果一致。

## sCOMET

用独立环境安装 [unbabel-comet 2.2.7](../configs/requirements-comet.txt)，取得 [WMT22 COMET-DA](https://huggingface.co/Unbabel/wmt22-comet-da) 的 `checkpoints/model.ckpt` 和同一下载目录的 `hparams.yaml`。评分器会检查正式 checkpoint SHA-256。需让 Hugging Face 缓存能解析对应 XLM-R encoder/tokenizer。把 `COMET_CHECKPOINT` 设为 checkpoint 路径。

```bash
for suite in general naev100 naev300; do
  python -m evaluate.reproduce metric --work outputs/rescore --suite "$suite" \
    --metric scomet --checkpoint "$COMET_CHECKPOINT" --gpus 0
done
```

正式运行用 batch size 8。可改用 `--gpus 1`。缺译按空串，超长候选清空后计分并独立记录；原文或参考自身超限会报告错误。

## Doc-COMET

使用独立 Python 3.9 环境。从 [doc-mt-metrics](https://github.com/amazon-science/doc-mt-metrics) checkout `5385cc28930aae9924edcb3201645dd3810b12c0`，设置 `DOC_MT_METRICS` 为其目录。可用 pip 23.3.2 安装旧版 Lightning 元数据。

```bash
python -m pip install pip==23.3.2
python -m pip install -r installation/requirements-doccomet.txt
python -m pip install --no-deps -e "$DOC_MT_METRICS/COMET"
export COMET_CHECKPOINT
export HF_HOME=/absolute/path/to/comet-hf-cache
python scripts/score_general_comet_dcomet.py --output outputs/rescore/general \
  --test-data outputs/rescore/general-test.json --gpus 0
```

`HF_HOME` 中须有 XLM-R 配置与 tokenizer 缓存。Doc-COMET 启用文档评分，读同篇前两句以内的源侧及离线参考历史。所有方法共享窗口及超限排除项。冻结结果中 News 覆盖 98/100，GuoFeng 覆盖 99/99。

## LTCR

安装 [LTCR 依赖](../installation/requirements-ltcr.txt)及 `en_core_web_sm==3.8.0`。从 [awesome-align](https://github.com/neulab/awesome-align) 检出 `5f150d45bbe51e167daf0a84abebaeb07c3323d1`。设置 `LTCR_TOOLS` 为含 `awesome-align/` 和本地 `mbert/` 目录的资产根，mBERT 使用多语言 cased BERT。同一环境需能导入 PyTorch 和 awesome-align，后者也可通过源代码路径导入。

```bash
export PYTHONPATH=.:src:scripts:"$LTCR_TOOLS/awesome-align"
for suite in naev100 naev300; do
  python -m evaluate.reproduce metric --work outputs/rescore --suite "$suite" --metric ltcr
done
```

每个重复专名以第一处资源中的译法为比较基准。LTCR-1 要求精确一致，LTCR-1f 也接受子串。包内汇总使用各条件共同可对齐的比较，Naev 100 为 24/38，Naev 300 为 349/417。

## 可选 LLM Judge

```bash
python3 scripts/run_llm_judge.py prepare --root . --output outputs/judge
```

此命令离线生成匿名评审请求。正式表中没有 Judge 分数。后续 `pilot` 或 `run` 使用脚本所配置的 Responses API 及独立的 `TOKENDANCE_API_KEY`，并会发送评审输入。评审提示词见 [prompts/judge](../prompts/judge)。
