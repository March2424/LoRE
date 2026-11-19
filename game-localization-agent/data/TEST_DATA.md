# Test data for the translation experiments

Each directory contains the exact source and reference translation used in
scoring. Its language files use the same document IDs and line order as the
corresponding files under [results](../results/).

| Test set | Data directory | Source files | Reference files |
|---|---|---|---|
| News Commentary v18.1 | `news_commentary/en-zh` | `en.nc_3`, `en.nc_6` | `zh.nc_3`, `zh.nc_6` |
| GuoFeng TEST_2 | `guofeng/zh-en` | `zh.gf_210_210`, `zh.gf_70_70` | `en.gf_210_210`, `en.gf_70_70` |
| Naev multi-model comparison | `naev/en-zh` | `en.0` | `zh.0` |
| Naev ablation | `naev_ablation_300/en-zh` | `en.0` | `zh.0` |

For example, `data/news_commentary/en-zh/zh.nc_3` is the reference for
`results/news_commentary/<model>/<method>/en-zh/zh.nc_3`. The test files
are shared across models and methods. The target-language files under `data`
are evaluation references and should be kept out of model inputs and memory.

Each UTF-8 text line corresponds to one resource. Embedded backslash, CR and LF
are escaped as `\\`, `\r` and `\n`, with the same encoding as `results`.
Use `records.json` for the exact unescaped `src` and `ref`, with the occurrence
ID, document ID and one-based physical line number. `test_manifest.json`
lists coverage, hashes and the paired results manifest.

Naev 100 is the prefix of Naev 300. The Naev files retain the upstream reference
status and fuzzy flag in `records.json`. See the snapshot's [attribution and
license notes](naev/snapshot-123fdc1/README.md). GuoFeng TEST_2 was used in
selector training. News includes all 100 test pairs, of which the original
dCOMET evaluation scored 98. All four exports contain the scored test sets;
Naev warm-up resources are outside those sets.
