"""检索质量基线/消融问题集（与行为评估 ``evaluation_dataset.py`` 互补）。

与行为评估的区别：本数据集用于测量**检索质量**（Hit@K / Recall@K / MRR / 拒答分布），
每条问题携带**原文锚点**（evidence quote）而不是 chunk ID —— 分块策略在 P3/P4 会变化，
引用必须按原文跨度映射，不能把旧 chunk ID 当作真值（见 POST_MVP_DEVELOPMENT_PLAN §6）。

问题集口径（P0 登记，禁止据测量结果下调）：
- 总量目标 ≥60（调参 30 / 独立验收 30）；当前为 P0 种子集（12 题），
  受"真实长文档完整摄取依赖 P2"限制，后续在 P3/P5 扩充到 60 并按 split 固定；
- 分层：术语、同义表达、中文缩写、数字/单位/否定、表格、跨页、开头/中段/末尾、
  章节总结、跨章比较、无答案；同一事实不得换问法跨 split。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RETRIEVAL_DATASET_VERSION = "retrieval-1"

Split = Literal["tuning", "holdout"]

# 当前语料中真实可检索的两份文档（P0 实测 available + 有 active version）。
DOC_STRUCTURED_OUTPUT = "尚硅谷-06-结构化输出.pdf"
DOC_AI_AGENTS = "AI-Agents-in-Depth-zh-CN-sample-20.pdf"


@dataclass(frozen=True)
class RetrievalQuestion:
    question_id: str
    split: Split
    family: str
    question: str
    document_titles: tuple[str, ...]
    # 原文锚点：任一命中即视为该片段被检索到；多片段用于计算完整证据 Recall。
    evidence_quotes: tuple[str, ...] = ()
    answerable: bool = True
    notes: str = ""


RETRIEVAL_QUESTIONS: tuple[RetrievalQuestion, ...] = (
    # ---- 调参集（tuning）----
    RetrievalQuestion(
        question_id="so-what-is-structured-output",
        split="tuning",
        family="term",
        question="LangChain 的结构化输出要求模型最终返回什么？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=("要求模型最终返回一个符合预定义结构的数据对象",),
    ),
    RetrievalQuestion(
        question_id="so-priority-enum-medium",
        split="tuning",
        family="enum",
        question="Priority 枚举中 MEDIUM 对应的中文取值是什么？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=('MEDIUM = "中"',),
    ),
    RetrievalQuestion(
        question_id="so-which-ways-validate",
        split="tuning",
        family="negation",
        question="四种获取结构化结果的方式里，除 Pydantic 外其余方式会校验字段吗？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=("其余三种方式不校验",),
    ),
    RetrievalQuestion(
        question_id="so-rating-description",
        split="tuning",
        family="number",
        question="示例中电影评分字段 rating 的满分是多少？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=("电影评分，满分十分",),
    ),
    RetrievalQuestion(
        question_id="so-comment-scenarios",
        split="tuning",
        family="list",
        question="用结构化输出处理用户评论的应用场景包括哪些？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=("批量处理用户评论", "自动生成分析报告", "发现产品改进点"),
    ),
    RetrievalQuestion(
        question_id="so-invoice-extraction",
        split="tuning",
        family="example",
        question="文档信息提取举例里用于发票信息的模型类叫什么？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=("发票信息",),
    ),
    # ---- 独立验收集（holdout）----
    RetrievalQuestion(
        question_id="aa-author-and-version",
        split="holdout",
        family="metadata",
        question="这本 AI Agent 书的作者与版本日期是什么？",
        document_titles=(DOC_AI_AGENTS,),
        evidence_quotes=("李博杰 著",),
    ),
    RetrievalQuestion(
        question_id="aa-sutton-stages",
        split="holdout",
        family="theory",
        question="Richard Sutton 说宇宙演化经历了哪几个阶段？",
        document_titles=(DOC_AI_AGENTS,),
        evidence_quotes=("从尘埃到恒星、从恒星到生命、从生命到智能体",),
    ),
    RetrievalQuestion(
        question_id="aa-chapter8-thesis",
        split="holdout",
        family="cross_section",
        question="第八章关于后训练的核心论点是什么？",
        document_titles=(DOC_AI_AGENTS,),
        evidence_quotes=("SFT记忆、RL泛化",),
    ),
    RetrievalQuestion(
        question_id="aa-high-risk-tool-principle",
        split="holdout",
        family="principle",
        question="高风险操作的专用工具设计需要额外加入什么？",
        document_titles=(DOC_AI_AGENTS,),
        evidence_quotes=("必要时再加上预览和人工确认",),
    ),
    RetrievalQuestion(
        question_id="aa-observation-action-space",
        split="holdout",
        family="concept",
        question="文中如何解释产品能力演进与观察空间、动作空间的关系？",
        document_titles=(DOC_AI_AGENTS,),
        evidence_quotes=("产品能力的演进往往就是观察空间和动作空间的演进",),
    ),
    RetrievalQuestion(
        question_id="unanswerable-office-room",
        split="holdout",
        family="unanswerable",
        question="尚硅谷宋红康老师的办公室门牌号是多少？",
        document_titles=(DOC_STRUCTURED_OUTPUT,),
        evidence_quotes=(),
        answerable=False,
        notes="语料中不存在的事实：用于记录 top-1 相似度分布，校准拒答阈值。",
    ),
)


def dataset_summary() -> dict[str, object]:
    families: dict[str, int] = {}
    for item in RETRIEVAL_QUESTIONS:
        families[item.family] = families.get(item.family, 0) + 1
    splits: dict[str, int] = {}
    for item in RETRIEVAL_QUESTIONS:
        splits[item.split] = splits.get(item.split, 0) + 1
    return {
        "dataset_version": RETRIEVAL_DATASET_VERSION,
        "question_count": len(RETRIEVAL_QUESTIONS),
        "splits": splits,
        "families": families,
        "target_size": 60,
    }


__all__ = [
    "RETRIEVAL_DATASET_VERSION",
    "RETRIEVAL_QUESTIONS",
    "RetrievalQuestion",
    "dataset_summary",
]
