"""
NovaMind 全局配置模块

职责：计算并创建所有工作目录路径，管理环境变量。
所有路径都基于项目根目录自动推导，支持通过环境变量覆盖。
"""
import os
from dotenv import load_dotenv

load_dotenv()

# 路径推导：从当前文件向上找到项目根目录
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(CORE_DIR)
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs")
HARNESS_DIR = os.path.join(PROJECT_ROOT, "harness")
POLICY_PATH = os.path.join(HARNESS_DIR, "policies.json")

# 工作空间根目录（支持环境变量覆盖）
WORKSPACE_DIR = os.getenv(
    "NOVAMIND_WORKSPACE",
    os.path.join(PROJECT_ROOT, "workspace")
)

# 子目录定义
DB_PATH = os.path.join(WORKSPACE_DIR, "state.sqlite3")       # 对话状态持久化（短期记忆）
MEMORY_DIR = os.path.join(WORKSPACE_DIR, "memory")            # 用户画像存储（长期记忆）
PROFILE_PATH = os.path.join(MEMORY_DIR, "user_profile.md")    # 当前用户画像文件
PROFILE_BACKUP_DIR = os.path.join(MEMORY_DIR, "profile_backups")  # 画像历史备份目录
PERSONAS_DIR = os.path.join(WORKSPACE_DIR, "personas")        # 人设模板区
SCRIPTS_DIR = os.path.join(WORKSPACE_DIR, "scripts")          # 自动化脚本区
OFFICE_DIR = os.path.join(WORKSPACE_DIR, "office")            # 沙盒工位（唯一允许执行文件操作的空间）
SKILLS_DIR = os.path.join(OFFICE_DIR, "skills")               # 动态技能插件目录
TASKS_FILE = os.path.join(WORKSPACE_DIR, "tasks.json")        # 定时任务队列

# 日志目录
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

# 自动创建所有必要目录
for d in [WORKSPACE_DIR, MEMORY_DIR, PROFILE_BACKUP_DIR, PERSONAS_DIR, SCRIPTS_DIR, OFFICE_DIR, SKILLS_DIR, LOG_DIR, DOCS_DIR, HARNESS_DIR]:
    os.makedirs(d, exist_ok=True)

# 日志级别配置
LOG_LEVEL = os.getenv("NOVAMIND_LOG_LEVEL", "INFO")
