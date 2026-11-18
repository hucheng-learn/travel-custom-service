import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict


POLICY_HINT = re.compile(r"政策|规则|退款.{0,12}(?:状态|进度|流水|多久到账|到账|记录|申请|审核|成功|批准)|"
                         r"退改|免费取消|怎么退|如何退|订单号.{0,16}(?:区别|还是)|酒店.{0,10}起价|使用指南")
MISSING_DEADLINE = re.compile(r"(?:退款|退回|退钱).*(?:到账|入账).*(?:多久|几天|几小时|何时)|"
                              r"(?:退款|退回|退钱).*(?:多久|几天|几小时|何时).*到账")


def looks_like_knowledge(text: str) -> bool:
    return POLICY_HINT.search(text) is not None


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

    def answer(self, text: str) -> tuple[Literal["knowledge", "clarify"], str]:
        query = _normalized(text)
        if item := self._faq.get(query):
            return "knowledge", self._cite(item.answer, item.section)
        if "退款" in text and re.search(r"这笔订单|我的订单|某笔订单|订单\s*[A-Za-z0-9]", text):
            return "clarify", "通用资料不能确认某笔订单的退款结果。请在中台核对这笔订单的退款申请和退款流水。"
        if MISSING_DEADLINE.search(text):
            return "knowledge", self._cite("现有资料没有提供到账时限，我不能确认具体多久。可以先核对退款申请和流水状态。", "查询退款进度")
        if query in {"退款", "政策", "这个政策是什么", "退改规则", "能退吗", "怎么退", "如何退", "退款政策"}:
            return "clarify", "你想查某个商品的退改条件，还是已提交退款的处理进度？请补充具体商品或问题。"

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
        if not scores or scores[0][0] < 1.2:
            return "knowledge", "现有知识资料里没有足够依据回答这个问题，我不会猜测。可以补充具体规则名称或商品。"
        if len(scores) > 1 and scores[1][0] >= scores[0][0] * 0.85:
            return "clarify", f"你问的是「{scores[0][1].section}」还是「{scores[1][1].section}」？"
        evidence = scores[0][1]
        return "knowledge", self._cite(evidence.text, evidence.section)
