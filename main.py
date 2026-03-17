from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os
from typing import Dict, List

from .core.utils import (
    load_reminders,
    restore_reminders,
    save_reminders,
    add_job,
    remove_job,
    remove_all_jobs_for_item,
    schedule_reminder,
    reschedule_reminder,
    get_platform_adapter_name,
    get_platform_api_client,
    check_recall_capability,
    send_aiocqhttp_with_message_id,
    recall_message_later,
    save_media_component,
)


class ReminderPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.scheduler = AsyncIOScheduler()
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_reminder")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "reminders.json")
        self.reminders: List[Dict] = []
        self.linked_tasks: Dict[str, List[Dict]] = {}
        self.job_mapping: Dict[str, Dict[str, str]] = {}
        load_reminders(self)
        self.whitelist = self.config.get('whitelist', [])
        self._recall_notice_sent: set[str] = set()

        logger.info("定时提醒助手已加载")

    def _is_allowed(self, event: AstrMessageEvent):
        """检查用户是否有权限使用该插件"""
        if event.is_admin():
            return True
        if not self.whitelist:
            return False
        return event.get_sender_id() in self.whitelist

    async def initialize(self):
        """初始化插件，启动调度器"""
        restore_reminders(self)
        self.scheduler.start()
        logger.info(f"定时提醒助手启动成功，已加载 {len(self.reminders)} 个提醒任务")

    # 委托给工具函数的方法（保持向后兼容）
    def _load_reminders(self):
        """从文件加载提醒数据"""
        load_reminders(self)

    def _save_reminders(self):
        """保存提醒数据到文件"""
        save_reminders(self)

    def _restore_reminders(self):
        """恢复所有提醒任务到调度器"""
        restore_reminders(self)

    def _add_job(self, item: Dict, session: str):
        """为指定会话添加任务到调度器"""
        add_job(self, item, session)

    def _remove_job(self, item: Dict, session: str):
        """移除指定会话的任务"""
        remove_job(self, item, session)

    def _remove_all_jobs_for_item(self, item: Dict):
        """移除某个提醒/任务在所有会话中的任务"""
        remove_all_jobs_for_item(self, item)

    async def _schedule_reminder(self, item: Dict):
        """调度提醒或任务"""
        await schedule_reminder(self, item)

    async def _reschedule_reminder(self, item: Dict):
        """重新调度提醒或任务"""
        await reschedule_reminder(self, item)

    def _get_platform_adapter_name(self, platform_id: str) -> str:
        """获取平台适配器名称"""
        return get_platform_adapter_name(self, platform_id)

    def _get_platform_api_client(self, platform_id: str):
        """获取平台 API 客户端"""
        return get_platform_api_client(self, platform_id)

    def _check_recall_capability(self, unified_msg_origin: str) -> tuple:
        """检查目标会话是否支持自动撤回"""
        return check_recall_capability(self, unified_msg_origin)

    async def _send_aiocqhttp_with_message_id(self, item: Dict, unified_msg_origin: str):
        """使用 aiocqhttp 发送消息并返回 message_id"""
        return await send_aiocqhttp_with_message_id(self, item, unified_msg_origin)

    async def _save_media_component(self, msg_comp, prefix: str):
        """保存媒体组件到本地"""
        return await save_media_component(self, msg_comp, prefix)

    async def _recall_message_later(self, unified_msg_origin: str, message_id, delay_seconds: int):
        """延迟撤回消息"""
        await recall_message_later(self, unified_msg_origin, message_id, delay_seconds)



    @filter.command("添加任务")
    async def add_task(self, event: AstrMessageEvent):
        """添加定时任务
        用法: /添加任务 <任务名称> [@好友号|#群号] <cron表达式> <指令>
        示例: /添加任务 每日签到 0 9 * * * /签到
        说明: cron表达式为5段格式：分 时 日 月 周
        💡 可以在发送指令的同时附上图片
        💡 不指定会话参数时，会自动发送到当前会话
        """
        from .core.task_manager import TaskManager
        task_manager = TaskManager(self)
        async for result in task_manager.add_task(event):
            yield event.plain_result(result)

    @filter.command("添加提醒")
    async def add_reminder(self, event: AstrMessageEvent):
        """添加定时提醒
        用法: /添加提醒 <提醒名称> [@好友号|#群号] <cron表达式> <消息内容> [图片]
        示例: /添加提醒 早安 0 9 * * * 早上好！
        说明: cron表达式为5段格式：分 时 日 月 周
        💡 可以在发送指令的同时附上图片，提醒时会一起发送文字和图片
        💡 支持在消息内容前添加撤回时间，如：/添加提醒 测试 0 9 * * * 2m测试消息
        💡 不指定会话参数时，会自动发送到当前会话
        """
        from .core.reminder_manager import ReminderManager
        reminder_manager = ReminderManager(self)
        async for result in reminder_manager.add_reminder(event):
            yield event.plain_result(result)

    @filter.command("编辑任务")
    async def edit_task(self, event: AstrMessageEvent):
        """编辑定时任务
        用法: /编辑任务 <任务名称或序号> [@好友号|#群号] [cron表达式] [指令]
        说明: 所有参数都可选，可单独或组合编辑，支持使用序号或名称
        示例1: /编辑任务 每日签到 0 8 * * * (只修改时间)
        示例2: /编辑任务 1 /新指令 (使用序号修改指令)
        示例3: /编辑任务 每日签到 0 8 * * * /新指令 (同时修改时间和指令)
        示例4: /编辑任务 每日签到 @123456 (只修改会话，覆盖原有会话)
        示例5: /编辑任务 2 @123456 0 8 * * * (使用序号同时修改会话和时间)
        💡 序号可通过 /查看任务 获取
        💡 编辑会话时会完全替换原有的会话列表，如需增量管理请使用启动/停止指令
        """
        from .core.task_manager import TaskManager
        task_manager = TaskManager(self)
        async for result in task_manager.edit_task(event):
            yield event.plain_result(result)

    @filter.command("编辑提醒")
    async def edit_reminder(self, event: AstrMessageEvent):
        """编辑定时提醒
        用法: /编辑提醒 <提醒名称或序号> [@好友号|#群号] [cron表达式] [消息内容]
        说明: 所有参数都可选，可单独或组合编辑，支持使用序号或名称
        示例1: /编辑提醒 早安 0 8 * * * (只修改时间)
        示例2: /编辑提醒 1 新的内容 (使用序号只修改内容)
        示例3: /编辑提醒 早安 0 8 * * * 新的内容 (同时修改时间和内容)
        示例4: /编辑提醒 早安 @123456 (只修改会话，覆盖原有会话)
        示例5: /编辑提醒 2 @123456 0 8 * * * 新的内容 (使用序号同时修改会话、时间和内容)
        💡 可以在发送指令的同时附上图片，提醒时会更新发送的文字和图片
        💡 支持在消息内容前添加撤回时间，如：2m测试消息
        💡 序号可通过 /查看提醒 获取
        💡 编辑会话时会完全替换原有的会话列表，如需增量管理请使用启动/停止指令
        """
        from .core.reminder_manager import ReminderManager
        reminder_manager = ReminderManager(self)
        async for result in reminder_manager.edit_reminder(event):
            yield event.plain_result(result)

    @filter.command("查看任务")
    async def list_tasks(self, event: AstrMessageEvent, name: str = ""):
        """查看定时任务
        用法1: /查看任务 - 查看所有任务列表
        用法2: /查看任务 <序号> - 查看指定序号任务的详细信息
        用法3: /查看任务 <任务名称> - 查看指定名称任务的详细信息
        """
        from .core.task_manager import TaskManager
        task_manager = TaskManager(self)
        async for result in task_manager.list_tasks(event, name):
            yield event.plain_result(result)

    @filter.command("查看提醒")
    async def list_reminders(self, event: AstrMessageEvent, name: str = ""):
        """查看定时提醒
        用法1: /查看提醒 - 查看所有提醒列表
        用法2: /查看提醒 <序号> - 查看指定序号提醒的详细信息（包含完整文字和图片）
        用法3: /查看提醒 <提醒名称> - 查看指定名称提醒的详细信息（包含完整文字和图片）
        """
        from .core.reminder_manager import ReminderManager
        reminder_manager = ReminderManager(self)
        async for result in reminder_manager.list_reminders(event, name):
            # 检查是否是消息链
            if isinstance(result, dict) and result.get("type") == "message_chain":
                yield event.chain_result(result["data"])
            else:
                yield event.plain_result(result)

    @filter.command("删除任务")
    async def delete_task(self, event: AstrMessageEvent, key: str = None):
        """删除定时任务
        用法: /删除任务 <序号或名称>
        示例1: /删除任务 1 (删除第1个任务)
        示例2: /删除任务 每日签到 (删除名为"每日签到"的任务)
        """
        if key is None:
            yield event.plain_result("❌ 参数缺失！\n用法: /删除任务 <序号或名称>")
            return
        from .core.task_manager import TaskManager
        task_manager = TaskManager(self)
        async for result in task_manager.delete_task(event, str(key).strip()):
            yield event.plain_result(result)

    @filter.command("删除提醒")
    async def delete_reminder(self, event: AstrMessageEvent, key: str = None):
        """删除定时提醒
        用法: /删除提醒 <序号或名称>
        示例1: /删除提醒 1 (删除第1个提醒)
        示例2: /删除提醒 早安 (删除名为"早安"的提醒)
        """
        if key is None:
            yield event.plain_result("❌ 参数缺失！\n用法: /删除提醒 <序号或名称>")
            return
        from .core.reminder_manager import ReminderManager
        reminder_manager = ReminderManager(self)
        async for result in reminder_manager.delete_reminder(event, str(key).strip()):
            yield event.plain_result(result)


    @filter.command("链接提醒")
    async def link_reminder_to_task(self, event: AstrMessageEvent):
        """链接提醒到任务，提醒执行后执行指定指令
        用法: /链接提醒 <提醒名称> <指令> [参数可选]
        示例: /链接提醒 早安 /签到
        说明: 当提醒「早安」执行后，会自动执行指令「/签到」
        💡 支持为同一个提醒链接多个指令，将按添加顺序依次执行
        """
        from .core.linked_task_manager import LinkedTaskManager
        linked_task_manager = LinkedTaskManager(self)
        async for result in linked_task_manager.link_reminder_to_task(event):
            yield event.plain_result(result)


    @filter.command("启动提醒", alias={"启用提醒"})
    async def enable_reminder(self, event: AstrMessageEvent):
        """启动定时提醒
        用法1: /启动提醒 <提醒名称> - 在当前会话启动该提醒
        用法2: /启动提醒 <提醒名称> [@好友号|#群号 ...] - 在指定会话启动该提醒
        示例: /启动提醒 早安 #123456 (在群123456中启动"早安"提醒)
        """
        from .core.reminder_manager import ReminderManager
        reminder_manager = ReminderManager(self)
        async for result in reminder_manager.toggle_reminder_session(event, enable=True):
            yield event.plain_result(result)

    @filter.command("停止提醒", alias={"终止提醒", "停用提醒"})
    async def disable_reminder(self, event: AstrMessageEvent):
        """停止定时提醒
        用法1: /停止提醒 <提醒名称> - 在当前会话停止该提醒
        用法2: /停止提醒 <提醒名称> [@好友号|#群号 ...] - 在指定会话停止该提醒
        示例: /停止提醒 早安 #123456 (在群123456中停止"早安"提醒)
        """
        from .core.reminder_manager import ReminderManager
        reminder_manager = ReminderManager(self)
        async for result in reminder_manager.toggle_reminder_session(event, enable=False):
            yield event.plain_result(result)

    @filter.command("启动任务", alias={"启用任务"})
    async def enable_task(self, event: AstrMessageEvent):
        """启动定时任务
        用法1: /启动任务 <任务名称> - 在当前会话启动该任务
        用法2: /启动任务 <任务名称> [@好友号|#群号 ...] - 在指定会话启动该任务
        示例: /启动任务 每日签到 #123456 (在群123456中启动"每日签到"任务)
        """
        from .core.task_manager import TaskManager
        task_manager = TaskManager(self)
        async for result in task_manager.toggle_task_session(event, enable=True):
            yield event.plain_result(result)

    @filter.command("停止任务", alias={"终止任务", "停用任务"})
    async def disable_task(self, event: AstrMessageEvent):
        """停止定时任务
        用法1: /停止任务 <任务名称> - 在当前会话停止该任务
        用法2: /停止任务 <任务名称> [@好友号|#群号 ...] - 在指定会话停止该任务
        示例: /停止任务 每日签到 #123456 (在群123456中停止"每日签到"任务)
        """
        from .core.task_manager import TaskManager
        task_manager = TaskManager(self)
        async for result in task_manager.toggle_task_session(event, enable=False):
            yield event.plain_result(result)

    @filter.command("查看链接")
    async def list_linked_tasks(self, event: AstrMessageEvent):
        """查看所有链接的任务
        用法1: /查看链接 - 显示所有提醒及其链接的任务
        用法2: /查看链接 <提醒名称> - 显示指定提醒的链接任务详情
        """
        from .core.linked_task_manager import LinkedTaskManager
        linked_task_manager = LinkedTaskManager(self)
        async for result in linked_task_manager.list_linked_tasks(event):
            yield event.plain_result(result)

    @filter.command("删除链接")
    async def delete_linked_task(self, event: AstrMessageEvent, reminder_index: int = None, command_index: int = None):
        """删除指定的链接任务
        用法1: /删除链接 - 交互式删除，显示所有链接任务列表
        用法2: /删除链接 <提醒序号> <任务序号> - 直接删除指定链接任务
        示例: /删除链接 1 1 (删除第1个有链接的提醒的第1个链接命令)
        """
        from .core.linked_task_manager import LinkedTaskManager
        linked_task_manager = LinkedTaskManager(self)
        async for result in linked_task_manager.delete_linked_task(event, reminder_index, command_index):
            yield event.plain_result(result)

    async def terminate(self):
        """插件卸载时强制清理所有任务"""
        # 关闭调度器
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        logger.info("定时提醒助手已彻底卸载并清理任务")
