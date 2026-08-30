"""
NovaMind 内置工具集

提供智能体的核心能力：
  - 时间查询
  - AST安全计算器（替代eval）
  - 用户画像管理
  - 系统模型信息查询
  - 定时任务CRUD
  - 沙盒文件操作
  - Shell命令执行
"""
from datetime import datetime
from .base import novamind_tool
import ast
import operator
import os
import shutil
import uuid
from .. import config as _config
from ..config import MEMORY_DIR, PROFILE_BACKUP_DIR
from .. import task_store
from ..task_store import TASKS_LOCK, load_tasks_unlocked, write_tasks_unlocked
from .sandbox_tools import (
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell,
)


# 画像历史备份保留上限
MAX_PROFILE_BACKUPS = 10


# ==================== AST安全计算器 ====================

# 允许的数学运算符映射
_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval_node(node: ast.AST) -> float:
    """
    递归解析AST节点，只允许安全的数学运算

    安全策略：
    - 只接受数字字面量和四则运算
    - 禁止函数调用、属性访问、变量引用等危险操作
    - 使用AST而非eval，从根本上杜绝代码注入
    """
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body)
    elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    elif isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"不支持的运算符: {op_type.__name__}")
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        return _SAFE_OPERATORS[op_type](left, right)
    elif isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _SAFE_OPERATORS:
            raise ValueError(f"不支持的一元运算符: {op_type.__name__}")
        operand = _safe_eval_node(node.operand)
        return _SAFE_OPERATORS[op_type](operand)
    else:
        raise ValueError(
            f"不支持的表达式类型: {type(node).__name__}。"
            "计算器只支持数字和四则运算（+, -, *, /, //, %, **）。"
        )


def safe_calculate(expression: str) -> float:
    """
    AST安全的数学表达式计算

    使用Python AST模块解析表达式，只允许数字和四则运算。
    比eval安全得多，因为不允许执行任意代码。

    Args:
        expression: 数学表达式字符串，如 "3 * (5 + 2)"

    Returns:
        计算结果

    Raises:
        ValueError: 表达式包含不安全的操作
        SyntaxError: 表达式语法错误
    """
    tree = ast.parse(expression, mode="eval")
    return _safe_eval_node(tree)


# ==================== 内置工具函数 ====================


@novamind_tool
def get_system_model_info() -> str:
    """
    获取当前 NovaMind 正在运行的底层大模型（LLM）型号和提供商信息。
    当用户询问"你是基于什么模型"、"你的底层大模型是什么"、"你是GPT还是GLM"等身份问题时，调用此工具。
    """
    provider = os.getenv("DEFAULT_PROVIDER", "unknown")
    model = os.getenv("DEFAULT_MODEL", "unknown")

    if provider == "unknown" or model == "unknown":
        return "无法获取当前的系统模型配置，可能是环境变量未正确加载。"

    return f"当前使用的模型提供商(Provider)是: {provider}，具体型号(Model)是: {model}。"


def _backup_profile_if_exists() -> None:
    """如果当前画像文件存在，备份到 PROFILE_BACKUP_DIR 并清理超额备份。"""
    if not os.path.exists(_config.PROFILE_PATH):
        return

    os.makedirs(PROFILE_BACKUP_DIR, exist_ok=True)
    # 含毫秒，消除同秒内连续保存合并为一个备份的问题
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = os.path.join(PROFILE_BACKUP_DIR, f"user_profile.{timestamp}.md")
    shutil.copy2(_config.PROFILE_PATH, backup_path)

    # 清理超额备份：按文件名时间戳排序，保留最近 MAX_PROFILE_BACKUPS 份
    backups = sorted(
        f for f in os.listdir(PROFILE_BACKUP_DIR)
        if f.startswith("user_profile.") and f.endswith(".md")
    )
    for old_backup in backups[:-MAX_PROFILE_BACKUPS]:
        try:
            os.remove(os.path.join(PROFILE_BACKUP_DIR, old_backup))
        except OSError:
            pass


@novamind_tool
def save_user_profile(new_content: str) -> str:
    """
    更新用户的全局显性记忆档案。
    当你发现用户的偏好发生改变，或者有新的重要事实需要记录时：
    1. 请先调用 read_user_profile 获取当前的完整档案。
    2. 在你的上下文中，将新信息融入档案，并删去冲突或过时的旧信息。
    3. 将修改后的一整篇完整 Markdown 文本作为 new_content 参数传入此工具。
    注意：此操作将完全覆盖旧文件！请确保传入的是完整的最新档案。
    """
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # 覆写前备份旧画像（如果存在）
    _backup_profile_if_exists()

    # 原子写入：先写临时文件，再 os.replace 覆盖目标文件
    tmp_path = _config.PROFILE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.replace(tmp_path, _config.PROFILE_PATH)

    # 桥接五层记忆：画像同时写入 procedural 记忆（若记忆 provider 已启用，否则静默降级）
    try:
        from ..memory.bootstrap import get_memory_provider
        from ..memory.persona import save_persona_to_memory

        provider = get_memory_provider()
        if provider is not None:
            save_persona_to_memory(provider.manager(), new_content, source="save_user_profile")
    except Exception:
        pass

    return "记忆档案已成功覆写更新。新的人设画像已生效。"


@novamind_tool
def get_current_time() -> str:
    """
    获取当前的系统时间和日期。
    当用户询问"现在几点"、"今天星期几"、"今天几号"等与当前时间相关的问题时，调用此工具。
    """
    now = datetime.now()
    return f"当前本地系统时间是: {now.strftime('%Y-%m-%d %H:%M:%S')}"


@novamind_tool
def calculator(expression: str) -> str:
    """
    AST安全的数学计算器（替代eval方案）。

    用于计算基础的数学表达式，例如: '3 * 5' 或 '100 / 4'。
    支持的运算符: +, -, *, /, //(整除), %(取余), **(幂运算)
    支持括号和负数，例如: '(3 + 4) * 2' 或 '-5 ** 2'

    安全说明：使用AST解析替代eval，从根本上杜绝代码注入风险。
    """
    try:
        result = safe_calculate(expression)
        # 如果结果是整数，显示为整数
        if result == int(result):
            result = int(result)
        return f"表达式 '{expression}' 的计算结果是: {result}"
    except ValueError as e:
        return f"计算出错: {str(e)}"
    except SyntaxError:
        return f"计算出错: 表达式 '{expression}' 语法不正确，请检查格式。"
    except ZeroDivisionError:
        return "计算出错: 除数不能为零。"
    except Exception as e:
        return f"计算出错，请检查表达式格式。错误信息: {str(e)}"


@novamind_tool
def schedule_task(
    target_time: str,
    description: str,
    repeat: str = None,
    repeat_count: int = None,
) -> str:
    """
    为一个未来的任务设定闹钟或提醒。
    参数 target_time 必须是严格的格式："YYYY-MM-DD HH:MM:SS"
    参数 description 是需要执行的动作或要说的话。

    【高级循环功能】：
    - repeat (可选): 设置重复频率。可选值为 "hourly", "daily", "weekly", "monthly"。
    - repeat_count (可选): 结合 repeat 使用，表示一共需要触发几次。

    【案例教学】：
    1. 用户说："以后每天8点提醒我喝牛奶" -> repeat="daily", repeat_count=None (无限循环)
    2. 用户说："接下来的3天，每天提醒我吃药" -> repeat="daily", repeat_count=3 (有限循环)
    3. 用户说："明早8点叫我起床" -> repeat=None, repeat_count=None (单次任务)

    【时间歧义严格确认协议 (AM/PM Ambiguity CRITICAL)】：
    当用户说出的时间存在 12 小时制的模糊性时（例如：只说了"7点"，没明确说早上还是晚上）：
    1. 你必须向用户提问确认是上午还是下午。
    2. 【死命令】：在用户明确回复"上午"或"下午"之前，本工具处于【绝对锁定状态】！
    3. 严禁出现"抱歉多问了"、"默认早上"这种妥协行为。
    """
    try:
        target_dt = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "设定失败：时间格式错误，必须严格遵循 'YYYY-MM-DD HH:MM:SS' 格式。"

    now = datetime.now()
    if target_dt <= now:
        return (
            "设定失败：target_time 必须晚于当前时间。"
            f" 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}，"
            f" 你传入的是：{target_time}"
        )

    with TASKS_LOCK:
        try:
            tasks = load_tasks_unlocked()
        except Exception as e:
            return f"设定失败：读取任务队列异常 {str(e)}"

        new_task = {
            "id": str(uuid.uuid4())[:8],
            "target_time": target_time,
            "description": description,
            "repeat": repeat,
            "repeat_count": repeat_count,
        }
        tasks.append(new_task)

        try:
            write_tasks_unlocked(tasks)
        except Exception as e:
            return f"设定失败：写入任务队列异常 {str(e)}"

    msg = f" 任务已成功加入队列。首发时间：{target_time} | 任务：{description}"
    if repeat:
        msg += f" | 循环模式：{repeat} (共 {repeat_count if repeat_count else '无限'} 次)"
    return msg


@novamind_tool
def list_scheduled_tasks() -> str:
    """
    查看当前所有待处理的定时任务列表。
    当用户询问"我都有哪些任务"、"查一下闹钟"、"刚才定了什么"时调用此工具。
    """
    with TASKS_LOCK:
        if not os.path.exists(task_store.TASKS_FILE):
            return "当前没有任何定时任务。"

        try:
            tasks = load_tasks_unlocked()

            if not tasks:
                return "当前没有任何定时任务。"

            tasks.sort(key=lambda x: x["target_time"])

            res = " 当前待执行任务列表：\n"
            for t in tasks:
                res += (
                    f"- [ID: {t['id']}] 时间: {t['target_time']} "
                    f"| 任务: {t['description']}\n"
                )
            return res
        except Exception as e:
            return f"查询失败：{str(e)}"


@novamind_tool
def delete_scheduled_task(task_id: str) -> str:
    """
    根据任务 ID 取消或删除一个定时任务。

    【强制性风险控制协议 (CRITICAL)】：
    删除操作具有不可逆性。
    1. 只要匹配到符合描述的任务数量 > 1。
    2. 无论用户语气多么确定，只要他没提供具体的任务 ID。
    你必须先列出所有匹配的任务，并询问用户确认。
    严禁自作主张执行批量删除。
    """
    with TASKS_LOCK:
        if not os.path.exists(task_store.TASKS_FILE):
            return "删除失败：任务列表文件不存在。"

        try:
            tasks = load_tasks_unlocked()

            new_tasks = [t for t in tasks if t["id"] != task_id]

            if len(new_tasks) == len(tasks):
                return f"删除失败：未找到 ID 为 {task_id} 的任务。"

            write_tasks_unlocked(new_tasks)

            return f" 任务 [ID: {task_id}] 已成功取消。"
        except Exception as e:
            return f"操作异常：{str(e)}"


@novamind_tool
def modify_scheduled_task(
    task_id: str, new_time: str = None, new_description: str = None
) -> str:
    """
    修改现有定时任务的时间或内容。

    【强制性风险控制协议 (CRITICAL)】：
    1. 只要用户通过"模糊描述"来要求修改，而没有直接提供 ID。
    2. 只要系统中匹配到的任务数量 > 1。
    你必须向用户展示匹配到的所有任务列表，并强制询问确认。
    """
    with TASKS_LOCK:
        if not os.path.exists(task_store.TASKS_FILE):
            return "修改失败：任务列表为空。"

        try:
            tasks = load_tasks_unlocked()

            found = False
            for t in tasks:
                if t["id"] == task_id:
                    if new_time:
                        parsed_new_time = datetime.strptime(
                            new_time, "%Y-%m-%d %H:%M:%S"
                        )
                        now = datetime.now()
                        if parsed_new_time <= now:
                            return (
                                "修改失败：new_time 必须晚于当前时间。"
                                f" 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}，"
                                f" 你传入的是：{new_time}"
                            )
                        t["target_time"] = new_time
                    if new_description:
                        t["description"] = new_description
                    found = True
                    break

            if not found:
                return f"修改失败：未找到 ID 为 {task_id} 的任务。"

            write_tasks_unlocked(tasks)

            return f" 任务 [ID: {task_id}] 已成功更新。"
        except ValueError:
            return "修改失败：时间格式错误。"
        except Exception as e:
            return f"操作异常：{str(e)}"


BUILTIN_TOOLS = [
    get_current_time,
    calculator,
    save_user_profile,
    list_office_files,
    read_office_file,
    write_office_file,
    execute_office_shell,
    get_system_model_info,
    schedule_task,
    list_scheduled_tasks,
    delete_scheduled_task,
    modify_scheduled_task,
]
