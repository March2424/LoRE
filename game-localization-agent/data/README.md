# 数据与来源

| 数据 | 源侧文件 | 用途 |
| --- | --- | --- |
| Flare v0.2，4,416 条 | [四列 CSV](flare/flare_entity_input_v0.2.csv) | 建图、游戏 Teacher、训练 |
| Shattered Pixel Dungeon v4.0.2，5,607 条 | [四列 CSV](shattered/v4.0.2/shattered_entity_input.csv) | 建图、游戏 Teacher、训练 |
| Naev `123fdc1`，36,572 条 | [四列 CSV](naev/snapshot-123fdc1/naev_entity_input.csv) | 建图与游戏留出评测 |
| News Commentary v18.1、GuoFeng V1 TEST_2 | [冻结评分数据](TEST_DATA.md) | 复评与对齐 |
| 自建两条示例 | [源侧 CSV](demo/benchmark.csv) | 离线及 API 冒烟验证 |

三个游戏各自保留原始署名、许可、资源位置和版本。见 [Flare](flare/v0.2/README.md)、[Shattered](shattered/v4.0.2/README.md)、[Naev](naev/snapshot-123fdc1/README.md)。训练发行版语料从原项目获取并按 [采样说明](../sample/README.md)准备。News 使用 [LoongDocMT](https://github.com/YutongWang1216/LoongDocMT) 的文档边界。GuoFeng V1 的来源和使用条件见 [GuoFeng-Webnovel](https://github.com/longyuewangdcu/GuoFeng-Webnovel) 与 [DocMTAgent](https://github.com/YutongWang1216/DocMTAgent)。

`graphs/source-graphs.tar.gz` 包含三个游戏及正式通用实验四个文档/章节的源侧 SQLite。图包含已登记的源文及可定位建图证据，为各实验的独立运行记忆提供起点。使用 [正式推理入口](../inference/README.md)解包并检查 Naev 与通用实验图的固定哈希。`graphs/tokenizer.json` 为该轮实验的预算 tokenizer。五类记忆与当前资源的作用域、语言方向和读水位绑定。

## 从原始快照重新导入

各快照的版本和路径见上方三个游戏的署名与发布清单。Naev 的适配器读取主仓库及匹配的 assets 快照并调用 GNU gettext。Shattered 的 Java 调用点解析依赖 tree-sitter。下面的入口将原始文件整理为可供建图的 CSV／JSONL。

```bash
python3 scripts/download_flare_sources.py
python3 scripts/build_flare_full.py --output outputs/imported/flare
python3 scripts/build_shattered.py --raw-root /path/to/shattered --output outputs/imported/shattered
python3 scripts/build_naev.py --raw-root /path/to/naev \
  --assets-root /path/to/naev-assets --output outputs/imported/naev
```

对自有双语 CSV，可按 `data/demo/localization.csv` 的四个列名填写后运行 `scripts/import_localization_csv.py`。它生成独立 `source.jsonl` 和 `references.jsonl`。若要建图，另按 `data/demo/benchmark.csv` 的列结构提供源侧 CSV。测试参考在离线阶段按 ID 对齐。完整的模型提取请求结构见 [提取协议](../docs/EXTRACTION_SCHEMA.md)。
