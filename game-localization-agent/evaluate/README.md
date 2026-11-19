# 评测与结果核验

在提交根目录设置 `PYTHONPATH=.:src:scripts`。

```bash
python3 -m evaluate.verify
python3 -m evaluate.reproduce prepare --output outputs/rescore
```



## BLEU-4

在安装 `sacrebleu==2.6.0` 的环境执行。

```bash
python3 -m evaluate.reproduce metric --work outputs/rescore --suite naev100 --metric bleu
python3 -m evaluate.reproduce metric --work outputs/rescore --suite naev300 --metric bleu
```



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

`HF_HOME` 中须有 XLM-R 配置与 tokenizer 缓存。Doc-COMET 启用文档评分，读同篇前两句以内的源侧及离线参考历史。所有方法共享窗口及超限排除项。

## LTCR

安装 [LTCR 依赖](../installation/requirements-ltcr.txt)及 `en_core_web_sm==3.8.0`。从 [awesome-align](https://github.com/neulab/awesome-align) 检出 `5f150d45bbe51e167daf0a84abebaeb07c3323d1`。设置 `LTCR_TOOLS` 为含 `awesome-align/` 和本地 `mbert/` 目录的资产根，mBERT 使用多语言 cased BERT。同一环境需能导入 PyTorch 和 awesome-align，后者也可通过源代码路径导入。

```bash
export PYTHONPATH=.:src:scripts:"$LTCR_TOOLS/awesome-align"
for suite in naev100 naev300; do
  python -m evaluate.reproduce metric --work outputs/rescore --suite "$suite" --metric ltcr
done
```

每个重复专名以第一处资源中的译法为比较基准。LTCR-1 要求精确一致，LTCR-1f 也接受子串。包内汇总使用各条件共同可对齐的比较。
