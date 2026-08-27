"""
NovaMind 心跳调度器

后台异步协程，定期检查定时任务队列（tasks.json）。
当任务到期时，将触发消息注入事件总线，由智能体处理。

支持的循环频率：
  - hourly: 每小时
  - daily: 每天
  - weekly: 每周
  - monthly: 每月

任务耗尽repeat_count后自动停止。
"""
import os
import asyncio
import calendar
from datetime import datetime, timedelta
from .task_store import TASKS_FILE, TASKS_LOCK, load_tasks_unlocked, write_tasks_unlocked


async def pacemaker_loop(
    task_queue: asyncio.Queue,
    check_interval: int = 10,
):
    """
    心跳主循环

    每隔 check_interval 秒检查一次任务队列。
    到期的任务会被推入 task_queue 供智能体消费。

    Args:
        task_queue: 事件队列（与主程序共享）
        check_interval: 检查间隔（秒）
    """
    while True:
        await asyncio.sleep(check_interval)

        if not os.path.exists(TASKS_FILE):
            continue

        now = datetime.now()
        pending_tasks = []
        triggered_tasks = []
        tasks = []  # 防御性初始化，避免NameError

        with TASKS_LOCK:
            try:
                tasks = load_tasks_unlocked()
            except Exception:
                continue

            if not tasks:
                continue

            for t in tasks:
                try:
                    target_dt = datetime.strptime(t["target_time"], "%Y-%m-%d %H:%M:%S")

                    if now >= target_dt:
                        triggered_tasks.append(t)

                        # 循环任务：计算下一次触发时间
                        repeat_freq = t.get("repeat")
                        if repeat_freq:
                            repeat_count = t.get("repeat_count")

                            # 检查是否还有剩余次数
                            if repeat_count is not None:
                                if repeat_count <= 1:
                                    continue
                                else:
                                    t["repeat_count"] = repeat_count - 1

                            # 计算下次触发时间
                            if repeat_freq == "hourly":
                                next_dt = target_dt + timedelta(hours=1)
                            elif repeat_freq == "daily":
                                next_dt = target_dt + timedelta(days=1)
                            elif repeat_freq == "weekly":
                                next_dt = target_dt + timedelta(days=7)
                            elif repeat_freq == "monthly":
                                month = target_dt.month + 1
                                year = target_dt.year
                                if month > 12:
                                    month = 1
                                    year += 1
                                last_day = calendar.monthrange(year, month)[1]
                                day = min(target_dt.day, last_day)
                                next_dt = target_dt.replace(year=year, month=month, day=day)
                            else:
                                continue

                            t["target_time"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
                            pending_tasks.append(t)
                    else:
                        # 未到期的任务继续保留
                        pending_tasks.append(t)
                except Exception:
                    pass

            # 将未触发的任务和续期后的循环任务写回文件
            if triggered_tasks:
                try:
                    write_tasks_unlocked(pending_tasks)
                except Exception:
                    pass

        # 将到期任务注入事件队列
        for t in triggered_tasks:
            system_msg = (
                f"【系统内部心跳触发】\n"
                f"你设定的定时任务已到期, 请立即主动提醒用户或执行动作。\n"
                f"任务内容: {t['description']}"
            )
            await task_queue.put(system_msg)
