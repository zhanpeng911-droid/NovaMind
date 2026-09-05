"""
NovaMind 全局配置模块

职责：计算并创建所有工作目录路径，管理环境变量。
所有路径都基于项目根目录自动推导，支持通过环境变量覆盖。
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── 路径推导：区分「源码资源根」与「运行时数据根」─────────────────────
# 源码资源根：docs / harness / policy 等只读资源，随包分发。
#   打包后（sys.frozen）这些资源被 pyinstaller 打进 exe，解压后 __file__
#   相对路径仍然有效，故始终基于 __file__ 推导。
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_DIR = os.path.dirname(_CORE_DIR)
SRC_ROOT = os.path.dirname(_PACKAGE_DIR)

# 运行时数据根：workspace / logs 等可写数据。
#   打包后若基于 __file__（onefile 临时解压目录），每次启动数据都会丢失。
#   exe 约定放在项目根的 dist/ 下，故 frozen 时数据根 = exe 上一级目录（即项目根），
#   与源码运行共享同一份数据；可用 NOVAMIND_DATA_ROOT 显式覆盖。
if getattr(sys, "frozen", False):
    _exe_dir = os.path.dirname(sys.executable)
    DATA_ROOT = os.getenv("NOVAMIND_DATA_ROOT") or os.path.dirname(_exe_dir)
else:
    DATA_ROOT = SRC_ROOT

# 兼容旧引用：PROJECT_ROOT 在开发模式下即源码根
PROJECT_ROOT = SRC_ROOT
DOCS_DIR = os.path.join(SRC_ROOT, "docs")
HARNESS_DIR = os.path.join(SRC_ROOT, "harness")
POLICY_PATH = os.path.join(HARNESS_DIR, "policies.json")

# 工作空间根目录（支持环境变量覆盖）
WORKSPACE_DIR = os.getenv(
    "NOVAMIND_WORKSPACE",
    os.path.join(DATA_ROOT, "workspace")
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
SKILL_DB_PATH = os.path.join(WORKSPACE_DIR, "skill_store.sqlite3")  # 三层技能 SQLite 存储
TASKS_FILE = os.path.join(WORKSPACE_DIR, "tasks.json")        # 定时任务队列

# 日志目录
LOG_DIR = os.path.join(DATA_ROOT, "logs")

# 自动创建所有必要目录
for d in [WORKSPACE_DIR, MEMORY_DIR, PROFILE_BACKUP_DIR, PERSONAS_DIR, SCRIPTS_DIR, OFFICE_DIR, SKILLS_DIR, LOG_DIR, DOCS_DIR, HARNESS_DIR]:
    os.makedirs(d, exist_ok=True)

# 日志级别配置
LOG_LEVEL = os.getenv("NOVAMIND_LOG_LEVEL", "INFO")

# 沙盒模式：第一版仅支持 local；docker 待镜像与真实 smoke test 就绪后启用，
# 未知值由 build_default_sandbox_provider() fail closed 拒绝。
SANDBOX_MODE = os.getenv("NOVAMIND_SANDBOX_MODE", "local").strip().lower()
