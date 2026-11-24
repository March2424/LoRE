# 《破碎的像素地牢》免安装演示数据库

克隆 LoRE、装好 [工作台依赖](../../../README.md#快速启动) 并构建前端后，在 `agentdesign` 运行以下命令。演示直接打开已准备好的 SQLite，无需安装游戏、导出文本或配置模型密钥。

```powershell
.\.venv\Scripts\python.exe demos/shattered/demo.py
```

macOS 和 Linux 使用 `.venv/bin/python demos/shattered/demo.py`。打开 [本地工作台](http://127.0.0.1:8766/)。端口占用时加 `--port 8767`。每次启动会把 [workbench.sqlite](workbench.sqlite) 复制到 `runs/shattered-demo/` 下的新目录，方便多次演示同一待复核任务。

## 演示路线

1. 在「Workspace / 工作台」选中 `Shattered Pixel Dungeon | EN → zh-Hans | Offline demo`。文件进度显示 600 条源文、3 条可交付译文、1 条待复核。
2. 在「Graph & Memory / 图谱与记忆」搜索 `corpse dust` 查看任务物品及杖匠台词，搜索 `hammer` 查看商人提到巨魔铁匠的原文证据。
3. 在「Jobs / 任务」查看预置翻译任务。小恶魔的告别语漏掉 `%s` 后完成自动修复；另一条漏掉 `%s` 的提交被硬检查拒绝，更正后获批。
4. 回「Workspace / 工作台」，在文件进度点开待复核队列，将 `Back` 的「后背」改为「返回」并批准。从该文件的「Validate & Export / 检查与导出」下载 CSV，确认已有 4 条交付译文。

## 数据来源与复现

数据来自仓库已有的 [Shattered Pixel Dungeon v4.0.2 双语快照](../../../game-localization-agent/data/shattered/v4.0.2/README.md)。数据库保存 600 个源资源、已发布图谱、失误与修复轨迹，以及三条演示时批准的译例。固定图谱边和模型回执只用于验证工作台流程。全部 600 条都有上游简中参考，参考集留在原目录，供离线评测。 [上游署名](../../../game-localization-agent/data/shattered/v4.0.2/ATTRIBUTION.txt) · [GPL-3.0-or-later 许可证](../../../game-localization-agent/data/shattered/v4.0.2/LICENSE.txt)

运行 `python demos/shattered/demo.py build` 可从仓库数据重建快照。构建会通过项目 API 导入、建图和创建任务，并从单个 SQLite 文件恢复到全新目录，复查人工批准前后的 CSV 导出。版本、记录统计和数据库 SHA-256 见 [snapshot.json](snapshot.json)。
