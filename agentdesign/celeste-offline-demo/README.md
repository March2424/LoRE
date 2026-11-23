# 《蔚蓝》英译简中离线演示包

此包基于 Steam build `17282758`。解压后使用 LoRE 工作台即可查看完整演示，无需安装《蔚蓝》或配置模型密钥。

## 启动

在已有 LoRE 仓库的 `agentdesign` 目录执行。先按 LoRE README 安装依赖并构建前端，再把下方脚本路径换成实际解压位置。

```powershell
.\.venv\Scripts\python.exe "D:\path\to\celeste-offline-demo\start_demo.py"
```

macOS 和 Linux 使用 `.venv/bin/python /path/to/celeste-offline-demo/start_demo.py`。打开 [本地工作台](http://127.0.0.1:8766/)。端口被占用时给命令加 `--port 8767`。每次启动使用独立数据库副本，可以重复演示同一待复核项。

## 现场演示

1. 在工作台查看项目里的 533 条源文、3 条可交付译文和 1 条待复核。
2. 在「图谱与记忆」搜索 `Badeline`、`Strawberries` 或 `Celestial Resort Hotel` 查看有原文证据的图谱关系。
3. 打开文件的待复核队列，将 `Clear` 的「清除」改为「通关」并批准。回到文件交付，下载含 4 条已批准译文的 CSV。

## 包内文件

- `workbench.sqlite` 保存演示项目、图谱、预置任务和批准记忆。
- `celeste-en-zh.csv` 是 LoRE 四列英文源文件，`references.jsonl` 是按键对应的官方简中参考。
- `manifest.json` 记录游戏版本、源文件哈希和数据统计，`acceptance.json` 记录固定回执验收结果。

预置图谱关系、译文和审校回执用于复现演示流程。真实模型评测可使用英文 CSV 和独立的参考文件。
