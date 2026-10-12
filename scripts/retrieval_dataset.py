"""Fixed 60-question source-anchored set; chapters 1-4 tuning, 7-10 holdout.

Boundary cases are scored separately. Candidate similarity is not refusal quality.
Questions were fixed before ablation; facts and quotes never cross the split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RETRIEVAL_DATASET_VERSION = "retrieval-2-ai313"
DOC_AI_AGENTS = "AI-Agents-in-Depth-zh-CN.pdf"
Split = Literal["tuning", "holdout"]


@dataclass(frozen=True)
class RetrievalQuestion:
    question_id: str
    split: Split
    family: str
    question: str
    document_titles: tuple[str, ...] = (DOC_AI_AGENTS,)
    evidence_quotes: tuple[str, ...] = ()
    answerable: bool = True
    notes: str = ""
    case_kind: Literal["retrieval", "no_answer", "scope", "incompatible"] = "retrieval"


# Each row names one distinct fact, question and required original spans.
_TUNING = (
    ("author", "metadata", "《深入理解AI Agent》的作者是谁？", ("李博杰 著",)),
    ("version", "date", "本书v2.0的版本日期是什么？", ("2026 年 9 月 23 日",)),
    (
        "sutton",
        "opening",
        "Sutton说宇宙演化经历哪三个阶段？",
        ("从尘埃到恒星、从恒星到生命、从生命到智能体",),
    ),
    ("whisper", "synonym", "作者用什么口述协作方式形成书稿？", ("whisper coding（口述式协作）",)),
    (
        "brain",
        "term",
        "Agent的大脑、眼睛、手脚分别是什么？",
        ("LLM 是大脑（决策核心），上下文是眼睛（决定它能看到什么），工具是手脚（决定它能做什么）",),
    ),
    (
        "mcp",
        "abbreviation",
        "MCP英文全称和中文名称是什么？",
        ("MCP（Model Context Protocol，模型上下文协议）",),
    ),
    ("highrisk", "condition", "高风险专用工具必要时需加入什么？", ("必要时再加上预览和人工确认",)),
    (
        "reasoning",
        "negation",
        "动态多步决策可选不支持思考的模型吗？",
        ("只要涉及多步思考或动态决策，就一定要选择支持思考的模型",),
    ),
    (
        "delay",
        "number_unit",
        "20轮推理每轮慢2秒，总等待增加多少秒？",
        ("20 轮推理，每轮慢 2 秒就意味着总共多等 40 秒",),
    ),
    (
        "system",
        "role",
        "system消息由谁编写，定义什么？",
        ("由开发者编写，定义 Agent 的身份、行为规则、约束条件",),
    ),
    (
        "toolrole",
        "api",
        "tool消息如何对应工具调用请求？",
        ("每条 tool 消息通过tool\\_call\\_id 与对应的工具调用请求关联",),
    ),
    (
        "toolsfield",
        "negation",
        "tools是第五种消息角色吗？",
        ("工具定义（tools）作为请求的独立字段（而非消息）",),
    ),
    (
        "stateless",
        "principle",
        "单轮API调用如何保存模型所需信息？",
        ("每次调用都是无状态的，所有模型需要的信息必须在请求的消息列表中完整提供",),
    ),
    (
        "cache",
        "cache",
        "Prompt Cache与KV Cache的关系是什么？",
        ("它是构建在推理引擎 KV Cache 之上的跨请求缓存",),
    ),
    (
        "timestamp",
        "example",
        "客服插入系统时间戳后首token延迟如何变化？",
        ("首 token 延迟从 0.5 秒涨到 3-5 秒",),
    ),
    ("append", "synonym", "时间、用户状态等易变化信息应放在哪里？", ("动态信息永远追加到末尾",)),
    (
        "chatformat",
        "constraint",
        "上下文设计为何要用标准API消息格式？",
        ("结构化消息会被 Chat Template 翻译成模型训练时见过的固定token 序列",),
    ),
    (
        "simple",
        "memory",
        "SimpleNotes记忆的最小单位是什么？",
        ("每条记忆是一个最小的、不可再分的事实",),
    ),
    ("jsoncards", "structure", "JSON Cards采用哪三层结构？", ("类别 → 子类别 → 键值对",)),
    ("chunk", "term", "分块Chunking如何处理长文档？", ("把长文档切成适合独立检索的片段",)),
    (
        "chunksize",
        "number_unit",
        "分块大小与重叠比例的实践起点是什么？",
        ("每块 256-1024 token、相邻块重叠 10%-20%",),
    ),
    ("dense", "synonym", "稠密检索如何表达文本语义相近？", ("语义相近的内容，向量距离也近",)),
    (
        "bow",
        "negation",
        "词袋模型是否考虑词序？",
        ("只关心哪些词出现了、出现了几次，完全忽略词序",),
    ),
    ("bm25", "term", "BM25比朴素TF-IDF加入什么修正？", ("词频饱和与长度归一化",)),
    (
        "rrf",
        "abbreviation",
        "RRF如何用得分，平滑常数通常是多少？",
        ("完全抛开原始得分、只看排名", "常取 60"),
    ),
    (
        "rerank",
        "comparison",
        "重排序能取代融合吗，各负责什么？",
        (
            "重排序并不替代融合",
            "融合负责从两路结果中产生统一的候选池，重排序负责在这个候选池上精排",
        ),
    ),
    (
        "summary",
        "chapter_summary",
        "概括第三章两种检索方式各自盲区。",
        ("稠密检索懂语义但可能漏掉关键词", "稀疏检索精确匹配但读不懂同义词"),
    ),
)

_HOLDOUT = (
    ("passk", "term", "Pass@k怎样定义一次通过？", ("只要至少有一次通过，任务就算通过",)),
    (
        "consecutive",
        "negation",
        "业务Pass连续k指标能允许偶尔失败吗？",
        ("要求每一次都通过，且不能触发安全、合规或幻觉等一票否决项",),
    ),
    (
        "environment",
        "definition",
        "可重复评估环境要求什么？",
        ("给定同一个初始状态，同一个 Agent 应当得到可比较的结果",),
    ),
    ("sftlimit", "condition", "SFT记忆、RL泛化是普遍属性吗？", ("不是 SFT 与 RL 的普遍属性",)),
    (
        "data",
        "chapter_summary",
        "第八章对算法与数据环境的主要判断是什么？",
        ("数据和环境，比算法更重要",),
    ),
    (
        "map",
        "analogy",
        "怎样用地图和指南针比较SFT与RL？",
        ("SFT 细致学习已有地图，RL 则可以拿着奖励这枚指南针探索地图外的候选路线",),
    ),
    (
        "rl",
        "abbreviation",
        "RL学习按情境选动作的目标是什么？",
        ("获得最大的累积奖励（Cumulative Reward）",),
    ),
    (
        "boundary",
        "principle",
        "强化学习中Agent和环境的边界是什么？",
        ("凡是 Agent 无法任意改变的，都属于环境",),
    ),
    (
        "format",
        "cross_page",
        "218页前后为何先SFT再RL稳定工具调用格式？",
        (
            "奖励函数连“成功还是失败”都判断不了",
            "用少量示范稳定格式与基本流程，使奖励",
            "可计算，再由 RL 优化策略",
        ),
    ),
    (
        "online",
        "comparison",
        "Online与On-policy是同一个概念吗？",
        (
            "Online（在线）只表示数据在训练过程中不断通过与环境交互产生",
            "On-policy（在轨）要求生成 rollout 的行为策略",
        ),
    ),
    (
        "policy",
        "condition",
        "在线rollout落后checkpoint为什么不一定在轨？",
        ("数据就已经陈旧，训练在统计意义上也已经带有 of-policy（离轨）成分",),
    ),
    (
        "updates",
        "table",
        "表9-2经验知识库的优势和局限是什么？",
        ("更新快、可追溯、可按需 检索", "依赖检索和模型正确应用"),
    ),
    (
        "carriers",
        "list",
        "持续进化各载体承载哪些能力？",
        (
            "事实和经验适合写成知识文档",
            "可以精确执行的流程与约束适合写成程序",
            "高维能力则必须进入模型参数",
        ),
    ),
    (
        "rawtrajectory",
        "negation",
        "能直接用原始轨迹作正式经验知识吗？",
        ("原始轨迹不适合作为正式知识单元",),
    ),
    (
        "evidence",
        "security",
        "原始网页和LLM摘要可直接当长期指令吗？",
        ("原始网页、工具输出及其 LLM 摘要都属于不可信证据",),
    ),
    (
        "confidence",
        "negation",
        "模型置信度能充当发布批准门槛吗？",
        ("模型给出的置信度也只是未经验证的估计，不能充当批准门槛",),
    ),
    (
        "validator",
        "security",
        "业务Agent能自改批准更新的验证器吗？",
        ("不能修改批准自身更新的验证器、测试用例、发布门槛、审计日志和稳定版本备份",),
    ),
    ("sleep", "synonym", "睡眠学习必须在夜间执行吗？", ("并不要求任务真的在夜间运行",)),
    (
        "context",
        "term",
        "共享上下文多Agent保留哪些历史？",
        ("后续 Agent 接收前一个 Agent 的完整对话历史和轨迹",),
    ),
    (
        "ipc",
        "abbreviation",
        "IPC通信有哪些基本范式？",
        ("共享内存（一方写入、另一方读取同一块存储）", "消息传递（把数据显式地发送给对方）"),
    ),
    (
        "topology",
        "list",
        "第十章有哪三种协作拓扑？",
        (
            "对等协作模式（Peer Collaboration Pattern）",
            "管理者模式（Orchestration Pattern）",
            "去中心化模式（Decentralized Pattern）",
        ),
    ),
    (
        "conflict",
        "conflict",
        "文件写入冲突和跨文件语义冲突如何区分？",
        (
            "后写入的那个把先写入的修改覆盖掉了",
            "文件层面看不出任何冲突，但多个 Agent 的操作在逻辑上相互矛盾",
        ),
    ),
    (
        "lock",
        "constraint",
        "乐观锁能防止所有跨文件语义冲突吗？",
        ("乐观锁只能防止同一文件的写入冲突",),
    ),
    (
        "vending",
        "tail",
        "Vending-Bench2模拟经营多长时间，怎样计分？",
        ("长达一个模拟年", "最终以账户余额计分"),
    ),
    (
        "rent",
        "example",
        "RentAHuman如何执行物理任务？",
        ("通过加密货币雇佣真人执行物理世界的任务",),
    ),
    (
        "fastchip",
        "number_unit",
        "后记TaalasHC1的生成速度和响应时间？",
        ("约 17000 token/s、响应低于 100 毫秒",),
    ),
    (
        "crosschapter",
        "cross_chapter",
        "第九章进化如何依赖前面知识系统和评估？",
        ("没有轨迹和知识系统，经验无处保存", "没有评估，系统更无法判断一次修改究竟是进步还是退化"),
    ),
)


def _questions() -> tuple[RetrievalQuestion, ...]:
    questions: list[RetrievalQuestion] = []
    for split, rows in (("tuning", _TUNING), ("holdout", _HOLDOUT)):
        for fact, family, question, quotes in rows:
            questions.append(
                RetrievalQuestion(
                    f"{split}-{fact}", split, family, question, evidence_quotes=quotes
                )
            )
        for kind, question in (
            (
                "no_answer",
                "作者的私人手机号码是多少？" if split == "tuning" else "作者的银行账户余额是多少？",
            ),
            (
                "scope",
                "检索未授权文档的内部数据。"
                if split == "tuning"
                else "检索不存在文档作用域中的财务信息。",
            ),
            (
                "incompatible",
                "查询与当前模型不兼容的知识索引。"
                if split == "tuning"
                else "只查询不匹配embedding profile的文档。",
            ),
        ):
            questions.append(
                RetrievalQuestion(
                    f"{split}-{kind}", split, kind, question, answerable=False, case_kind=kind
                )
            )
    return tuple(questions)


RETRIEVAL_QUESTIONS = _questions()


def dataset_summary() -> dict[str, object]:
    families: dict[str, int] = {}
    splits: dict[str, int] = {}
    for item in RETRIEVAL_QUESTIONS:
        families[item.family] = families.get(item.family, 0) + 1
        splits[item.split] = splits.get(item.split, 0) + 1
    return {
        "dataset_version": RETRIEVAL_DATASET_VERSION,
        "question_count": len(RETRIEVAL_QUESTIONS),
        "splits": splits,
        "families": families,
        "target_size": 60,
        "split_policy": "disjoint chapter facts: 1-4 tuning; 7-10/afterword holdout",
    }
