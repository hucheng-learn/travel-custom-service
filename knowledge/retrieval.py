import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


def _normalized(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text.casefold())


def _tokens(text: str) -> list[str]:
    # 首版用中文二字词切分，搭配章节的同义检索词，避免额外的分词模型依赖。
    terms = []
    for segment in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", text.casefold()):
        if segment.isascii() or len(segment) == 1:
            terms.append(segment)
        else:
            terms.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return terms


class FAQ(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    aliases: list[str]
    answer: str
    section: str


@dataclass(frozen=True)
class Evidence:
    section: str
    text: str
    terms: Counter[str]


class KnowledgeProvider:
    # 对应技术设计 3.4 右侧：查“酒店免费取消条件”时，读取本地 FAQ 和指南章节。
    # 检索回答本身不调用生成模型；结果附资料标题、章节和版本，无法确认具体订单的实时事实。
    def __init__(self, faq_file: Path, guide_file: Path) -> None:
        raw = guide_file.read_text(encoding="utf-8")
        header, *sections = re.split(r"^## ", raw, flags=re.MULTILINE)
        self._title = header.splitlines()[0].removeprefix("# ").strip()
        self._version = next(line.split("：", 1)[1] for line in header.splitlines() if line.startswith("版本："))
        self._documents = []
        for section in sections:
            heading, body = section.split("\n", 1)
            keywords, _, content = body.strip().partition("\n")
            self._documents.append(Evidence(heading.strip(), content.strip(), Counter(_tokens(heading + keywords + content))))
        self._faq: dict[str, FAQ] = {}
        headings = {item.section for item in self._documents}
        for raw_item in yaml.safe_load(faq_file.read_text(encoding="utf-8")):
            item = FAQ.model_validate(raw_item)
            if item.section not in headings:
                raise ValueError(f"FAQ 来源章节不存在：{item.section}")
            for wording in [item.question, *item.aliases]:
                self._faq[_normalized(wording)] = item
        self._average_length = sum(item.terms.total() for item in self._documents) / len(self._documents)

    def _cite(self, answer: str, section: str) -> str:
        return f"{answer}\n来源：《{self._title}》·{section}（v{self._version}）"

    def answer(self, text: str, *, topic: str = "general") -> tuple[Literal["knowledge", "clarify"], str]:
        # 3.4：语义分类已由规划器完成，这里只按检索证据回答，不再用关键词决定业务意图。
        # 具体退款状态走 refund_status；通用到账时限读取受限说明，不推断某笔申请已到账。
        if topic == "refund_arrival":
            return "knowledge", self._cite("现有资料没有提供固定到账时限，无法确认具体多久。请核对退款申请和流水状态。", "查询退款进度")
        query = _normalized(text)
        if item := self._faq.get(query):
            return "knowledge", self._cite(item.answer, item.section)
        terms = {term for term in _tokens(text) if term not in {"怎么", "如何", "请问", "这个", "平台", "你们"}}
        scores = []
        doc_count = len(self._documents)
        for evidence in self._documents:
            hits = terms & evidence.terms.keys()
            if len(hits) < 2:
                continue
            # BM25 兼顾词在本章节的频率和区分度，防止长章节靠字多排在前面。
            normalizer = 1.2 * (0.25 + 0.75 * evidence.terms.total() / self._average_length)
            score = 0.0
            for term in hits:
                frequency = evidence.terms[term]
                doc_frequency = sum(term in item.terms for item in self._documents)
                inverse = math.log1p((doc_count - doc_frequency + 0.5) / (doc_frequency + 0.5))
                score += inverse * frequency * 2.2 / (frequency + normalizer)
            scores.append((score, evidence))
        scores.sort(key=lambda item: item[0], reverse=True)
        # 3.4 有证据才回答：首名不足阈值则说明依据不足；前两名过于接近则先问章节。
        # 唯一且充分时返回命中章节的原有内容和来源，不推断资料没有给出的到账期限。
        if not scores or scores[0][0] < 1.2:
            return "knowledge", "现有知识资料里没有足够依据回答这个问题，我不会猜测。可以补充具体规则名称或商品。"
        if len(scores) > 1 and scores[1][0] >= scores[0][0] * 0.85:
            return "clarify", f"你问的是「{scores[0][1].section}」还是「{scores[1][1].section}」？"
        evidence = scores[0][1]
        return "knowledge", self._cite(evidence.text, evidence.section)
