"""ContractCompiler — 从 skill 文本自动编译 contract 规则。"""

from __future__ import annotations

import re

from ..types import ContractRule

_RE_PARAGRAPH_LIMIT = re.compile(r"(不超过|少于|最多|within|less than|at most)\s*(\d+)\s*(段|paragraph)", re.IGNORECASE)


class ContractCompiler:
    def compile(self, skill_content: str) -> list[ContractRule]:
        rules: list[ContractRule] = [
            ContractRule("nonempty", kind="programmatic", hard=True, description="SKILL.md body 非空"),
            ContractRule("json_parseable", kind="programmatic", hard=True, description="frontmatter YAML 可解析"),
        ]

        corpus = skill_content.lower()
        if self._mentions_sources(corpus):
            rules.append(ContractRule("must_cite", kind="programmatic", hard=False, description="skill 声明引用来源"))
        if self._mentions_conclusion_first(corpus):
            rules.append(ContractRule("lead_with_conclusion", kind="programmatic", hard=False, description="skill 声明先给结论"))
        if self._paragraph_limit(corpus) > 0:
            rules.append(ContractRule("paragraph_limit", kind="programmatic", hard=False, description="段落数限制", params={"max": self._paragraph_limit(corpus)}))

        rules.append(ContractRule("no_unfounded_claims", kind="programmatic", hard=False, description="无绝对化无据声明"))
        rules.append(ContractRule("semantic_density", kind="programmatic", hard=False, description="指令性词密度合理"))
        return rules

    @staticmethod
    def _mentions_sources(corpus: str) -> bool:
        return any(k in corpus for k in ("引用来源", "标注来源", "注明来源", "cite sources", "with sources", "source-backed"))

    @staticmethod
    def _mentions_conclusion_first(corpus: str) -> bool:
        return any(k in corpus for k in ("先给结论", "结论在前", "先说结论", "answer first", "lead with the conclusion"))

    @staticmethod
    def _paragraph_limit(corpus: str) -> int:
        for match in _RE_PARAGRAPH_LIMIT.finditer(corpus):
            try:
                return max(1, int(match.group(2)))
            except Exception:
                continue
        return 0
