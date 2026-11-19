# 新版上传译文重评记录

输入为 /root/autodl-tmp/projectdata_newversion。目录 data/ 保留本批经过哈希核对的测试集，incoming_results/ 保存原始上传译文及随附的旧元数据，results/ 保存重建后可用的结果，scores/ 保存三套评估器输入、逐条分数及汇总。

与上一轮修订稿相比，29 条译文发生变化，全部属于通用领域。相对初始模型输出共有 172 条变化。Naev 300 条 SFT/DPO 的原始上传第 69–70 行属于同一条资源，合并时保留两行文字和换行。逐条修订审计见 [revision.json](../results/revision.json)。

全部 38 组按上一轮相同的评估器重新评分。逐条核对了新 records.json、译文文本、检查点与文档行数。两套 Naev 的分数与上一轮一致（数值容差内）。本次输出位于新隔离目录，上传目录和上一版目录均保留。
