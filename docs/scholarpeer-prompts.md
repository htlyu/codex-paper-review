# ScholarPeer 提示词来源与改编说明

本项目的多阶段审查提示词改编自 ScholarPeer 论文附录 G，目标是为作者生成有证据、可核对的论文自查材料。以下映射说明各角色的来源及改动，不代表原作者的官方实现、认可或效果认证。这些提示词也不是 PAT 官方提示词。实际提示词位于 [paper_review_service/prompts](../paper_review_service/prompts)，其中 [common.md](../paper_review_service/prompts/common.md) 是本项目为各角色添加的共同约束。

## 来源与许可

- 作者：**Palash Goyal, Mihir Parmar, Yiwen Song, Hamid Palangi, Tomas Pfister, Jinsung Yoon**。
- 使用版本：**arXiv:2601.22638v2，2026 年 5 月 9 日**；本项目核对日期为 2026 年 9 月 22 日。
- v2 PDF 与 HTML 正文题名：*ScholarPeer: A Multi-Agent Framework for Automated Peer Review*。arXiv 摘要页题名为 *ScholarPeer: A Context-Aware Multi-Agent Framework for Automated Peer Review*；两者对应同一 arXiv 版本，特此保留题名差异。
- 原始材料：[版本记录及作者信息](https://arxiv.org/abs/2601.22638v2)、[v2 PDF](https://arxiv.org/pdf/2601.22638v2)、[v2 HTML](https://arxiv.org/html/2601.22638v2)。
- 许可：arXiv 版本页的许可链接指向 **Creative Commons Attribution 4.0 International（CC BY 4.0）**：[许可说明](https://creativecommons.org/licenses/by/4.0/)、[许可全文](https://creativecommons.org/licenses/by/4.0/legalcode)。归属声明另见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

本项目根据 PDF 第 32–39 页的附录 G 核对角色模板，并参考第 3 节的流程说明；附录 H 位于第 39–44 页。这里的页码均按 PDF 物理页计数，从 1 开始。核对时，arXiv HTML 中 G/H 仅显示小节标题和简短说明，未呈现提示词框正文，因此不能单凭 HTML 标题宣称已取得完整原文。

仓库保存的是**适配版本与来源映射**，不分发整篇论文，也不提供声称逐字完整的附录转录件。原文以链接中的固定 v2 PDF 为准。

## 附录 G 与本项目角色的映射

| 原文位置 | 本项目角色 | 原文职责与输入关系 | 本项目的适配重点 |
| --- | --- | --- | --- |
| G.1 Summarizer Agent，第 32 页 | [summary](../paper_review_service/prompts/summary.md) | 从论文全文提炼贡献、方法与主要结果，供后续角色理解论文。 | 建立带 PDF 页码的主张与证据记录，区分作者声称的结果和已核对的事实。 |
| G.2 Literature Review Agent，第 32–33 页 | [literature_review](../paper_review_service/prompts/literature_review.md) | 从题名、作者和摘要识别领域，检索截止日期前的基础工作、数据集、近期方法、竞争工作与综述，输出领域分析和文献 JSON；此阶段负责收集事实。 | 根据主张检索一手来源，记录链接、日期、相关性和检索局限，不预设批评结论。 |
| G.3 Literature Expansion Agent，第 34 页 | [literature_expansion](../paper_review_service/prompts/literature_expansion.md) | 读取已有文献列表，寻找基础工作、数据集来源及时间覆盖的空缺，只返回新增且不重复的文献。 | 对实际缺口补查，保留去重与截止日期约束，明确仍未解决的检索缺口。 |
| G.4 Sub-Domain Historian Agent，第 34–35 页 | [historian](../paper_review_service/prompts/historian.md) | 将完整文献记录整理为领域演变、未解决问题与贡献重要性的判断依据。 | 让领域叙述能够回溯到来源，避免从少量检索结果推出整个领域的共识。 |
| G.5 Baseline Scout Agent，第 35 页 | [baseline_scout](../paper_review_service/prompts/baseline_scout.md) | 独立读取论文中的任务、数据集和已有基线，检索可能遗漏的比较方法与数据集并说明相关性。 | 先判断任务、数据和评价设置是否可比；不预设作者隐瞒，不把每个未比较的方法都视作必要基线。 |
| G.6 Question Generator Agent，第 35–37 页 | [novelty_questions](../paper_review_service/prompts/novelty_questions.md)、[technical_questions](../paper_review_service/prompts/technical_questions.md) | 新颖性分支从贡献主张生成检索问题；其他维度分支生成针对指定维度的问题。两者均接收论文全文、摘要、领域叙述、文献与基线信息。 | 分开提出新颖性问题和技术问题，使问题对应具体主张及其支持材料。原文其他维度标题包含 Soundness、Clarity；本项目不另设独立的 clarity 阶段。 |
| G.7 Answer Generator Agent，第 37–38 页 | [novelty_answers](../paper_review_service/prompts/novelty_answers.md)、[technical_answers](../paper_review_service/prompts/technical_answers.md) | 新颖性分支结合问题、摘要与领域背景进行外部检索；其他维度分支结合论文全文和已有背景回答问题。 | 新颖性回答注明检索范围及证据限制；技术回答优先核对论文内部的方法、实验、数学与结论。答案不是自动成立的最终问题。 |
| G.8 Review Generator Agent，第 38–39 页 | [synthesis](../paper_review_service/prompts/synthesis.md) | 汇总论文、摘要和问答记录，并接收审稿指南与 few-shot 示例。 | 依据经复核的发现生成中文作者自查报告和结构化结果，给出定位、影响、证据与可执行修改建议。 |
| 本项目新增，无附录 G 对应角色 | [countercheck](../paper_review_service/prompts/countercheck.md) | 在问答后、报告合成前，对候选发现检查支持证据和反证。 | 排除误读、重复和不可比的文献批评；保留尚需作者确认的事项及其限制。 |

这些映射覆盖 G.1–G.8 的角色类别。它们不表示把原文每条指令照搬，也不表示复现了论文实验中的全部配置。

## 流程关系与执行边界

原文先建立论文摘要和外部文献背景，再将文献交给 historian、将论文交给 scout；问题生成综合这些信息，回答生成按新颖性与技术可靠性分工，最后合成审稿意见。G.3 是已有文献检索的补充，G.4 是背景整理，G.5 是独立比较检查，三者不能只换角色名称后重复同一段评论。第 3 节对新颖性回答和技术回答的取证方式也作了区分。[流程依据：第 3 节](https://arxiv.org/html/2601.22638v2#S3)

本项目按以下顺序串行调度 11 个阶段：`summary`、`literature_review`、`literature_expansion`、`historian`、`baseline_scout`、`novelty_questions`、`technical_questions`、`novelty_answers`、`technical_answers`、`countercheck`、`synthesis`。每个阶段由独立的 `codex exec` 调用执行，通过阶段 JSON 文件向下游传递结果，并保存各阶段日志；阶段内不再派生子 agent。串行执行用于避免共享登录状态并发刷新的冲突。

各阶段沿用服务配置的模型与推理强度，仅按职责设置搜索能力。程序增加结构验证，并在任务总时间额度内为后续阶段保留执行时间。`run.json` 记录阶段执行信息、中间结果哈希与提示词包哈希。这些调度方式、JSON 契约、预算分配、复核阶段以及中文报告格式均由本项目实现；它们不是 PAT 自适应难度预算机制的复现。

11 个阶段是本项目的执行划分，不能视作原论文的调用次数。原文附录 D.1 将调用成本写成固定步骤、文献扩展轮数及问答数量的组合，并描述了多轮扩展与逐问回答的实验配置；这里没有据此复现其模型、搜索 API、调用数量、时延、成本或基准效果。

## 关键改编

- **证据不足保持未决。** G.7 在未找到显著先前工作时要求评为高新颖性；本项目不采用这一规则。检索未发现只说明在已记录的范围内未找到，不能证明首创。
- **不输出录取判断。** G.8 将不确定性导向低分和拒稿；本项目不采用这一规则，也不生成审稿分数或接收／拒绝建议。证据不足的事项须保留为待确认项。
- **不凑固定数量。** 不强制 G.2 的 30–50 篇文献、G.4 的 300–400 词、G.6 的固定问题数或 G.7 的 2–3 篇支持文献。检索与问题数量由实际主张、覆盖缺口和可用证据决定。
- **不设顶会白名单。** 保留文献截止日期和可核查来源要求，优先使用与问题相关的一手论文及官方材料；不把原文的顶会／arXiv 范围当作排除领域期刊、专业会议或其他有效一手来源的规则。也不把 G.5 的近三年范围当作排除必要基础工作的硬限制。
- **区分相关与可比。** 删除 G.5 对作者隐瞒的预设。缺少某个基线是否构成问题，需要验证研究目标、数据、设置、指标、资源约束及论文实际主张。
- **重写输入输出契约。** 提示词被改为适用于本服务的中文说明、文件输入与结构化输出，增加来源、页码、状态、局限和反证要求。原附录的示例不是可直接执行的统一 schema：例如 G.2 与 G.3 使用不同的数据／性能字段名，G.5 的示例存在括号缺失；本项目采用自己的 JSON 契约。
- **保留时间边界。** 使用任务指定的 `cutoff_date` 约束先前工作；服务的默认截止日期见 [README](../README.md)。这不等于自动识别原稿投稿日期。进行历史时点评估时，应明确提供对应日期。

## 为什么附录 H 不进入论文审查流程

附录 H 的对象是**生成出来的审稿意见质量**，不是用户论文的待检查内容。三个模板均不作为上面 11 个阶段的角色提示词：

| 原文位置 | 所需输入与用途 | 与当前服务的边界 |
| --- | --- | --- |
| H.1 H-Max Score，第 39–41 页 | 论文、一份 AI 审稿意见与同一论文的人类审稿意见集合；比较 AI 发现相对于人类证据的增量。 | 用户只上传论文时不具备人类基准，不计算或宣称 H-Max 得分。 |
| H.2 Side-by-Side Evaluation，第 41–43 页 | 论文与两份审稿意见；按维度比较 A/B/Tie。第 4.1 节还规定隐藏系统身份、随机呈现顺序。 | 需要单独设计盲测并提供两份结果，不能让当前审查流程自行判定优于某系统。 |
| H.3 Summary of Gains Analysis，第 43–44 页 | 多个样本的 SxS 评估记录与正确恢复的系统对应关系；归纳跨样本的优势和不足。 | 单篇自查报告不具备这些输入，不据此声称系统具有论文中报告的优势。 |

未来若开展方法比较，应建立独立评估流程、准备相应输入并报告实验条件。当前的来源改编与程序运行本身不构成质量等效或性能复现证据。
