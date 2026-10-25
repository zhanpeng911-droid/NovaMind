"""Cheap local hints and explicit policy gates, with no model or filesystem IO."""
from __future__ import annotations

import hashlib
import os
import re

DEFAULTS = {"enabled": False, "min_samples": 3, "retention_days": 30,
            "max_observations": 1000, "cooldown_days": 7}
FAMILIES = {
    "paper": ("论文", "文献", "研究摘要", "paper"),
    "report": ("报告", "周报", "月报", "report"),
    "code": ("代码", "编程", "脚本", "程序", "code"),
    "study": ("复习", "学习计划", "课程", "错题"),
    "planning": ("日程", "计划", "会议安排", "待办"),
    "writing": ("写作", "文案", "文章", "设计稿"),
    "data": ("表格", "excel", "数据分析", "数据整理"),
    "design": ("界面设计", "设计界面", "图标设计", "配色", "设计方案"),
    "files": ("文件整理", "整理文件", "批量命名", "归档"),
    "translation": ("翻译", "译文"),
}
LABELS = {"paper": "论文与文献整理", "report": "报告整理", "code": "代码与脚本",
          "study": "学习与复习", "planning": "计划与安排", "writing": "写作与设计",
          "data": "表格与数据整理", "design": "设计工作方法", "files": "文件整理方法", "translation": "翻译方法"}


class LearningError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def can_observe(settings: dict, context: dict, auto_extract: bool) -> bool:
    return bool(settings.get("enabled") and auto_extract
                and context.get("memory_mode", "normal") == "normal")


def model_allowed() -> bool:
    flag = os.getenv("NOVAMIND_SKILL_EVOLUTION")
    return flag is None or flag.strip().lower() in {"1", "true", "on", "yes"}


def features(text: str) -> dict | None:
    lowered = text.lower().strip()
    if len(lowered) < 6:
        return None
    family = next((key for key, words in FAMILIES.items() if any(w in lowered for w in words)), None)
    if not family:
        return None
    # Family matches are suggestions only; never infer a successful procedure.
    return {"family": family, "label": LABELS[family], "fingerprint": digest(lowered)}


def skill_name(title: str, scope: str) -> str:
    return "learned-" + digest(scope + "\0" + title)[:16]


def redact_source(text: str) -> str:
    """Minimize common credentials and instance paths before preview/model use."""
    text = re.sub(r"sk-[\w-]{12,}|Bearer\s+[A-Za-z0-9._~-]{12,}", "[凭据已移除]", text, flags=re.I)
    text = re.sub(r"((?:api[_ -]?key|password|密码|secret|token)\s*[:=：]\s*)[^\s,;，；]+",
                  r"\1[已移除]", text, flags=re.I)
    return re.sub(r"[A-Za-z]:[\\/][^\s\"<>]+|/(?:Users|home)/[^\s\"<>]+", "{{文件路径}}", text)


def template(title: str) -> str:
    return (f"## 适用任务\n{title}\n\n## 不适用条件\n请补充适用边界。\n\n"
            "## 输入参数\n请列出需要用户提供的材料；信息不足先询问。\n\n"
            "## 执行步骤\n1. 请根据选定示例补充可复用的方法。\n\n"
            "## 输出格式\n请补充固定结构。\n\n## 完成检查\n请补充可核对标准。\n\n"
            "## 常见失败与处理\n缺少证据时明确说明，不编造结果。\n")


def static_check(body: str) -> list[str]:
    missing = [name for name in ("适用任务", "不适用条件", "输入参数", "执行步骤", "输出格式", "完成检查", "常见失败")
               if name not in body]
    if len(body) > 16000 or len(body.strip()) < 80:
        missing.append("正文长度须为 80–16000 字符")
    if re.search(r"sk-[\w-]{12,}|api[_ -]?key\s*[:=]|密码\s*[:：]|[A-Za-z]:[\\/]|/Users/", body, re.I):
        missing.append("请移除凭据或实例文件路径")
    if re.search(r"忽略.{0,8}(权限|系统|安全)|绕过.{0,8}(权限|沙箱)|无需.{0,6}用户确认", body):
        missing.append("技能不能扩大权限或绕过确认")
    if "请补充" in body or "请根据选定示例补充" in body:
        missing.append("仍有未填写的模板内容")
    return missing
