/* Display translations only: skill files, IDs and model-facing metadata stay intact. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.NovaMindSkillDescriptions = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const translations = {
    'Structured peer-review of academic papers.': '对学术论文进行结构化评审。',
    'Search arXiv papers by keyword, author, category, or ID.': '按关键词、作者、学科分类或编号搜索 arXiv 论文。',
    'Systematic literature review across multiple arXiv papers.': '综合多篇 arXiv 论文，开展系统性文献综述。',
    'Systematic multi-angle web research methodology.': '从多个角度开展系统性的网络调研。',
    'Consulting-grade research reports: framework + final report.': '搭建分析框架，撰写咨询级研究报告。',
    'Analyze Excel/CSV files with DuckDB SQL via bash.': '通过命令行使用 DuckDB SQL 分析 Excel 和 CSV 数据。',
    'Public-records OSINT: SEC, sanctions, courts, property.': '检索与分析证券监管、制裁、法院和房产等公开记录。',
    'Research and write professional newsletters.': '调研资料并撰写专业通讯。',
    'Multi-round deep research on any GitHub repo via API.': '通过 GitHub API 对代码仓库开展多轮深入研究。',
    'ML paper pipeline: experiment design to submission.': '协助机器学习论文从实验设计、撰写到投稿的完整流程。',
    'Monitor blogs and RSS/Atom feeds via blogwatcher-cli.': '通过 blogwatcher-cli 跟踪博客及 RSS、Atom 订阅更新。',
    'Create distinctive, production-grade frontend interfaces.': '设计并实现有辨识度、可用于实际产品的前端界面。',
    'Minimal SVG diagrams as standalone HTML files.': '制作简洁的 SVG 示意图，保存为独立 HTML 文件。',
    'Generate charts: select type, extract data, render image.': '选择合适图表、提取数据并生成可视化图像。',
    'Dark-themed SVG architecture/cloud/infra diagrams as HTML.': '以 HTML 展示深色风格的架构、云服务与基础设施 SVG 图。',
    'Generate PPTX presentations from slide plan + content.': '根据演示大纲与内容生成 PPTX 幻灯片。',
    'Generate docs: README, API reference, architecture, guides.': '编写 README、API 参考、架构说明与使用指南。',
    'Inspect codebases: LOC, languages, ratios via pygount.': '使用 pygount 分析代码行数、语言分布与代码占比。',
    'Debug Python: pdb REPL + debugpy remote (DAP).': '使用 pdb 和 debugpy 调试 Python，支持交互与远程调试。',
    'Debug Node.js via --inspect + Chrome DevTools Protocol.': '通过 --inspect 与 Chrome 开发者工具协议调试 Node.js。',
    'Clone, create, fork repos; manage remotes, releases.': '克隆、创建和复刻仓库，管理远程地址与版本发布。',
    'GitHub PR lifecycle: branch, commit, open, CI, merge.': '处理 GitHub 拉取请求流程：分支、提交、创建、持续集成与合并。',
    'Create, triage, label, assign GitHub issues via gh or REST.': '通过 gh 或 REST API 创建、分类、标记和分配 GitHub 议题。',
    'GitHub auth setup: HTTPS tokens, SSH keys, gh CLI.': '配置 GitHub 认证，包括 HTTPS 令牌、SSH 密钥与 gh 命令行。',
    'Execute plans task-by-task with two-stage review.': '逐项执行计划，并通过两阶段审查检查结果。',
    'Discover available skills in the builtin library by keyword.': '按关键词查找内置技能库中的可用技能。',
    'Onboarding conversation to generate a user profile.': '通过初次交流了解用户，整理个人偏好与背景。',
    'Review PRs: diffs, inline comments via gh or REST.': '通过 gh 或 REST API 审查代码差异并添加行内评论。',
    'Pre-commit review: security scan, quality gates, auto-fix.': '在提交前进行安全检查、质量验证与自动修复。',
    'Author SKILL.md: frontmatter, structure, writing principles.': '编写 SKILL.md 的元数据、文档结构与技能指导内容。',
    'Plan mode: write markdown plan, no execution.': '制定 Markdown 实施计划，规划阶段不执行修改。',
    'Create, edit, and evaluate agent skills iteratively.': '迭代创建、编辑与评估 Agent 技能。',
    'Throwaway experiments to validate an idea before build.': '在正式开发前，用小型实验验证想法的可行性。',
    'Sequential 3-lens cleanup of recent code changes.': '从三个角度依次审查并精简近期代码改动。',
    '4-phase root cause debugging: understand bugs before fixing.': '通过四阶段根因分析理解问题，再实施修复。',
    'TDD: enforce RED-GREEN-REFACTOR, tests before code.': '实践测试驱动开发：先写测试，再实现功能并重构。',
  };
  return {describe: description => translations[String(description || '').trim()] || description || '暂无简介'};
});
