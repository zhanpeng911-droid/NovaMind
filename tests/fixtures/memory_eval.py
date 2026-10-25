"""B3 记忆评测标准样本集。

- PREFERENCES: 30 条可验证的明确偏好（memory, query）
- DISTRACTORS: 30 条无关/诱导查询（不应注入任何偏好）
- CROSS_SESSION: 10 组跨会话场景（memory, query, source_thread）
- RACE_CASES: 20 组删除/拒绝/禁用/在飞竞态场景（action, expected_recallable）
"""

from __future__ import annotations

PREFERENCES: list[tuple[str, str]] = [
    ("偏好用中文回答", "请用中文回答这个问题"),
    ("解释代码时先讲整体思路", "解释这段代码，先讲思路"),
    ("回答先给结论再展开", "先给结论再解释"),
    ("代码示例优先用 Python", "给一个 Python 代码示例"),
    ("回答里不要使用表情符号", "回答不要用表情符号"),
    ("技术术语保留英文原文", "技术术语保留英文"),
    ("对比信息优先用表格", "用表格对比这两个方案"),
    ("步骤说明要编号", "把步骤用编号列出来"),
    ("回答尽量简短", "简短回答即可"),
    ("原理要详细解释", "详细解释一下原理"),
    ("结论要给出引用来源", "给出引用来源"),
    ("不确定时明确说不确定", "不确定就说不确定，不要编造"),
    ("测试框架偏好 pytest", "测试用 pytest 写"),
    ("前端偏好 TypeScript", "前端用 TypeScript"),
    ("包管理偏好 pnpm", "包管理用 pnpm"),
    ("本地环境用 Docker Compose", "本地用 Docker Compose 启动"),
    ("数据库偏好 PostgreSQL", "数据库用 PostgreSQL"),
    ("接口风格偏好 REST", "接口设计用 REST"),
    ("编辑器偏好 Vim 键位", "编辑器用 Vim 键位"),
    ("界面偏好深色主题", "界面用深色主题"),
    ("会议安排在周一上午", "会议安排在周一上午"),
    ("工作沟通优先用邮件", "工作沟通用邮件"),
    ("开发先写测试", "开发先写测试"),
    ("提交拆成小步", "提交拆成小步"),
    ("代码注释用中文", "代码注释用中文"),
    ("日志用结构化 JSON", "日志用结构化 JSON"),
    ("错误信息要给出下一步", "错误信息给出下一步操作"),
    ("动手前先确认范围", "动手前先确认范围"),
    ("不要自动修改文件", "不要自动修改文件"),
    ("Python 环境用 uv", "Python 环境用 uv 管理"),
]

DISTRACTORS: list[str] = [
    "今天天气怎么样", "推荐一部科幻电影", "股票行情如何", "附近有什么好吃的",
    "如何挑选跑鞋", "周末去哪里露营", "咖啡怎么冲更好喝", "帮我写一首诗",
    "旅行行李清单", "哪款相机适合拍风景", "健身房怎么选", "怎么养多肉植物",
    "手机电池不耐用", "如何学习游泳", "推荐几个播客", "晚饭吃什么",
    "怎么练字", "房间怎么布置", "如何挑选床垫", "养猫需要注意什么",
    "怎么提高睡眠质量", "推荐一款耳机", "如何做面包", "城市周边骑行路线",
    "怎么拍夜景", "如何整理衣柜", "推荐一部纪录片", "怎么选咖啡豆",
    "如何缓解肩颈酸痛", "周末做什么手工",
]

CROSS_SESSION: list[tuple[str, str, str]] = [
    ("偏好中文回答", "请用中文回答", "session-a"),
    ("回答先给结论", "先给结论", "session-b"),
    ("代码示例用 Python", "给个 Python 示例", "session-c"),
    ("测试用 pytest", "测试框架选什么", "session-d"),
    ("前端用 TypeScript", "前端语言用 TypeScript", "session-e"),
    ("数据库用 PostgreSQL", "数据库选 PostgreSQL", "session-f"),
    ("日志用结构化 JSON", "日志格式用结构化 JSON", "session-g"),
    ("提交拆成小步", "提交拆成小步", "session-h"),
    ("不要自动修改文件", "不要自动修改文件", "session-i"),
    ("Python 环境用 uv", "Python 环境用 uv", "session-j"),
]

# (action, expected_recallable)
RACE_CASES: list[tuple[str, bool]] = [
    ("delete", False), ("delete", False), ("delete", False),
    ("delete_then_migrate", False), ("delete_then_new_service", False),
    ("disable", False), ("disable", False), ("disable_then_enable", True),
    ("disable_then_supersede", False), ("disable_then_migrate", False),
    ("reject", False), ("reject", False), ("reject_then_propose", False),
    ("reject_then_migrate", False), ("reject_then_confirm", False),
    ("worker_inflight_delete", False), ("worker_inflight_delete", False),
    ("worker_inflight_reject", False), ("worker_inflight_disable", False),
    ("worker_inflight_delete", False),
]
