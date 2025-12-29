from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import json
import os
from typing import Dict, List
import aiohttp
import shutil
import asyncio
import time

from .core.command_trigger import CommandTrigger

@register("astrbot_plugin_reminder", "Foolllll", "支持在指定会话定时发送消息或执行任务，支持cron表达式、富媒体消息", "1.0.1")
class ReminderPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.scheduler = AsyncIOScheduler()
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_reminder")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "reminders.json")
        self.reminders: List[Dict] = []
        self.linked_tasks: Dict[str, List[str]] = {}  # {reminder_name: [task_command1, task_command2, ...]}
        self._load_reminders()
        self.monitor_timeout = self.config.get('monitor_timeout', 60)
        self._running_triggers = set()
        logger.info("定时提醒插件已加载")

    async def initialize(self):
        """初始化插件，启动调度器"""
        self._restore_reminders()
        self.scheduler.start()
        logger.info(f"定时提醒插件启动成功，已加载 {len(self.reminders)} 个提醒任务")

    def _load_reminders(self):
        """从文件加载提醒数据"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 兼容旧版本数据格式
                    if isinstance(data, list):
                        self.reminders = data
                        self.linked_tasks = {}
                    else:
                        self.reminders = data.get('reminders', [])
                        # 兼容旧版数据结构，将单个字符串转换为列表
                        old_linked_tasks = data.get('linked_tasks', {})
                        self.linked_tasks = {}
                        for reminder_name, task_data in old_linked_tasks.items():
                            if isinstance(task_data, str):
                                # 旧版数据：单个字符串
                                self.linked_tasks[reminder_name] = [task_data]
                            elif isinstance(task_data, list):
                                # 新版数据：列表
                                self.linked_tasks[reminder_name] = task_data
                            else:
                                # 其他情况，设为空列表
                                self.linked_tasks[reminder_name] = []
            except Exception as e:
                logger.error(f"加载提醒数据失败: {e}")
                self.reminders = []
                self.linked_tasks = {}
        else:
            self.reminders = []
            self.linked_tasks = {}

    def _save_reminders(self):
        """保存提醒数据到文件"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                data = {
                    'reminders': self.reminders,
                    'linked_tasks': self.linked_tasks
                }
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存提醒数据失败: {e}")

    def _restore_reminders(self):
        """恢复所有提醒任务到调度器"""
        for item in self.reminders:
            try:
                self._add_job(item)
            except Exception as e:
                logger.error(f"恢复提醒任务失败: {e}, 任务: {item}")

    def _add_job(self, item: Dict):
        """添加任务到调度器"""
        job_id = item['id']
        cron_expr = item['cron']

        parts = cron_expr.split()
        if len(parts) != 5:
            raise ValueError(f"无效的cron表达式: {cron_expr}")

        minute, hour, day, month, day_of_week = parts
        if item.get('is_task', False):
            job_func = self._execute_task
        else:
            job_func = self._send_reminder

        self.scheduler.add_job(
            job_func,
            CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week
            ),
            args=[item],
            id=job_id,
            replace_existing=True
        )

    async def _send_reminder(self, item: Dict):
        """发送提醒消息"""
        try:
            unified_msg_origin = item.get('unified_msg_origin')
            if not unified_msg_origin:
                logger.warning(f"无法发送提醒 '{item.get('name', 'unknown')}'，unified_msg_origin 未设置")
                return

            # 按照原始顺序构建消息
            chain = []
            for msg_item in item['message_structure']:
                if msg_item['type'] == 'text':
                    chain.append(Plain(msg_item['content']))
                elif msg_item['type'] == 'image':
                    full_path = os.path.join(self.data_dir, msg_item['path'])
                    if os.path.exists(full_path):
                        chain.append(Image.fromFileSystem(full_path))
                    else:
                        logger.warning(f"图片文件不存在: {full_path}")

            if not chain:
                logger.warning(f"提醒消息为空: {item['name']}")
                return

            message_chain = MessageChain()
            message_chain.chain = chain
            await self.context.send_message(unified_msg_origin, message_chain)

            logger.info(f"提醒已发送: {item['name']} -> {unified_msg_origin}")

            linked_commands = self.linked_tasks.get(item['name'], [])
            if linked_commands:
                # 并发执行所有链接任务
                tasks = []
                for linked_command in linked_commands:
                    task = self._execute_linked_command(linked_command, unified_msg_origin, item)
                    tasks.append(task)

                if tasks:
                    # 并发执行所有链接任务
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"发送提醒失败: {item.get('name', 'unknown')}, {e}", exc_info=True)


    async def _execute_command_common(self, command: str, unified_msg_origin: str, item: Dict, task_type: str = "task"):
        """执行命令的通用方法，用于任务和链接任务
        Args:
            command: 要执行的命令
            unified_msg_origin: 消息发送的目标
            item: 任务或提醒的配置项
            task_type: 任务类型，"task" 或 "linked_command"
        """
        logger.info(f"检测到{task_type}，执行: {command}")
        trigger = CommandTrigger(self.context, {"monitor_timeout": self.monitor_timeout})
        
        # 创建任务并记录
        task = asyncio.create_task(trigger.trigger_and_forward_command(unified_msg_origin, item, command))
        self._running_triggers.add(task)
        
        try:
            await task # 等待监控结束
            logger.info(f"{task_type}执行完成: {item['name']} -> {command}")
        except Exception as cmd_error:
            logger.error(f"执行{task_type}错误: {cmd_error}")
        finally:
            self._running_triggers.discard(task) # 任务结束移除记录
        
    async def _execute_linked_command(self, linked_command: str, unified_msg_origin: str, item: Dict):
        """执行单个链接任务"""
        await self._execute_command_common(linked_command, unified_msg_origin, item, "链接任务")

    async def _execute_task(self, item: Dict):
        """执行定时任务"""
        try:
            unified_msg_origin = item.get('unified_msg_origin')
            if not unified_msg_origin:
                logger.warning(f"无法执行任务 '{item.get('name', 'unknown')}'，unified_msg_origin 未设置")
                return

            command = item.get('command', '')
            if not command:
                logger.warning(f"任务 '{item.get('name', 'unknown')}' 没有指令")
                return

            logger.info(f"执行任务指令: {command} -> {unified_msg_origin}")

            await self._execute_command_common(command, unified_msg_origin, item, "任务")
        except Exception as e:
            logger.error(f"执行任务失败: {item.get('name', 'unknown')}, {e}", exc_info=True)

    async def _add_task_or_reminder(self, event: AstrMessageEvent, is_task: bool = False):
        """内部方法：添加任务或提醒的通用逻辑"""
        if is_task:
            command_name = "任务"
            example_usage = "/添加任务 每日签到 0 9 * * * /签到"
        else:
            command_name = "提醒"
            example_usage = "/添加提醒 早安 0 9 * * * 早上好！"

        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result(f"❌ 此指令仅限Bot管理员使用")
            return

        try:
            # 解析文本参数
            parts = event.message_str.strip().split(maxsplit=2)

            if len(parts) < 3:
                yield event.plain_result(
                    f"格式错误！\n"
                    f"用法1（当前会话）: /添加{command_name} <{command_name}名称> <cron表达式(5段)> <{'指令' if is_task else '消息内容'}>\n"
                    f"用法2（指定群聊）: /添加{command_name} <{command_name}名称> @<群号> <cron表达式(5段)> <{'指令' if is_task else '消息内容'}>\n"
                    f"cron表达式格式: 分 时 日 月 周\n"
                    f"示例1: /添加{command_name} 早安 0 9 * * * {'/签到' if is_task else '早上好！'}\n"
                    f"示例2: /添加{command_name} 早安 @123456 0 9 * * * {'/签到' if is_task else '早上好！'}\n"
                    f"{'💡 指令需以指令前缀开头，允许空格接参数' if is_task else '💡 可以在发送指令的同时附上图片，提醒时会一起发送文字和图片'}\n"
                    f"💡 不指定群号时，会自动发送到当前会话"
                )
                return

            _, name, remaining = parts

            # 检查名称是否重复
            for existing_item in self.reminders:
                if existing_item['name'] == name:
                    yield event.plain_result(f"❌ {command_name}名称 '{name}' 已存在，请使用不同的名称")
                    return

            # 尝试解析是否包含目标群号（格式如 @123456）
            remaining_parts = remaining.split(maxsplit=1)
            if len(remaining_parts) >= 2 and remaining_parts[0].startswith('@'):
                # 格式2：指定了目标群号
                group_id = remaining_parts[0][1:]  # 去掉 @ 符号
                remaining = remaining_parts[1]

                # 构建 unified_msg_origin
                # 从当前会话中提取平台信息
                current_origin = event.unified_msg_origin
                if ':' in current_origin:
                    platform = current_origin.split(':')[0]
                    unified_msg_origin = f"{platform}:GroupMessage:{group_id}"
                    logger.info(f"检测到目标群号: {group_id}, 构建会话ID: {unified_msg_origin}")
                else:
                    yield event.plain_result("❌ 无法识别当前平台信息，请使用当前会话模式")
                    return
            else:
                # 格式1：使用当前会话
                unified_msg_origin = event.unified_msg_origin
                logger.info(f"使用当前会话ID: {unified_msg_origin}")

            # 解析cron表达式（需要5段）
            # 使用 maxsplit=5 来分割，前5段是cron表达式，剩余的都是内容
            remaining_parts = remaining.split(maxsplit=5)

            if len(remaining_parts) < 5:
                yield event.plain_result(
                    "cron表达式格式错误！需要5段: 分 时 日 月 周\n"
                    "示例: 0 9 * * * 表示每天9点0分"
                )
                return

            cron_parts = remaining_parts[:5]

            last_part = cron_parts[4]
            cleaned_last_part = ''

            for i, char in enumerate(last_part):
                if char in '0123456789*-,/':
                    if char.isdigit():
                        digit_count = 1
                        for j in range(i + 1, min(i + 10, len(last_part))):
                            if last_part[j].isdigit():
                                digit_count += 1
                            else:
                                break
                        if digit_count > 3:
                            break
                    cleaned_last_part += char
                else:
                    break

            if not cleaned_last_part:
                yield event.plain_result(
                    "cron表达式格式错误！第5段（周）无效\n"
                    "示例: 0 9 * * * 表示每天9点0分"
                )
                return

            cron_parts[4] = cleaned_last_part
            cron_expr = ' '.join(cron_parts)

            content_text = ""
            if len(remaining_parts) > 5:
                content_text = remaining_parts[5]
            if len(last_part) > len(cleaned_last_part):
                content_text = last_part[len(cleaned_last_part):] + (' ' + content_text if content_text else '')

            content_text = content_text.strip()

            # 验证cron表达式
            try:
                CronTrigger.from_crontab(cron_expr)
            except Exception as e:
                logger.error(f"cron表达式验证失败: {e}")
                yield event.plain_result(f"cron表达式无效: {e}")
                return

            # 根据是否是任务验证内容
            if is_task:
                if not content_text:
                    yield event.plain_result(f"❌ 任务指令不能为空")
                    return
            else:
                # 提取完整的消息结构（图文混排）- 仅提醒需要
                message_structure = []
                message_chain = event.get_messages()

                # 遍历消息链，在 Plain 中找到 cron 表达式的结束位置
                cron_found = False

                for msg_comp in message_chain:
                    if isinstance(msg_comp, Plain):
                        if not cron_found and cron_expr in msg_comp.text:
                            # 找到了 cron 表达式
                            cron_index = msg_comp.text.index(cron_expr)
                            cron_end = cron_index + len(cron_expr)

                            # 提取 cron 之后的文本
                            content = msg_comp.text[cron_end:]
                            cron_found = True

                            if content.strip():
                                message_structure.append({
                                    "type": "text",
                                    "content": content
                                })
                        elif cron_found:
                            # 已经找到 cron，后续文本直接添加
                            if msg_comp.text.strip():
                                message_structure.append({
                                    "type": "text",
                                    "content": msg_comp.text
                                })

                    elif isinstance(msg_comp, Image):
                        # 图片只在找到 cron 之后添加
                        if cron_found:
                            img_filename = f"img_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
                            img_path = os.path.join(self.data_dir, img_filename)

                            try:
                                saved = False
                                if msg_comp.url:
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(msg_comp.url) as resp:
                                            if resp.status == 200:
                                                with open(img_path, 'wb') as f:
                                                    f.write(await resp.read())
                                                saved = True
                                elif msg_comp.file:
                                    shutil.copy(msg_comp.file, img_path)
                                    saved = True

                                if saved:
                                    message_structure.append({
                                        "type": "image",
                                        "path": img_filename
                                    })
                            except Exception as e:
                                logger.error(f"保存图片失败: {e}")

                # 验证至少有消息内容
                if not message_structure:
                    yield event.plain_result("提醒内容不能为空，请至少提供文字或图片")
                    return

            # 创建对象
            item_id = f"{'task' if is_task else 'reminder'}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(self.reminders)}"
            item = {
                'id': item_id,
                'name': name,
                'unified_msg_origin': unified_msg_origin,
                'cron': cron_expr,
                'is_task': is_task,
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'created_by': event.get_sender_id()
            }

            if is_task:
                item['command'] = content_text
            else:
                item['message_structure'] = message_structure  # 保存完整的消息结构

            # 添加到调度器
            self._add_job(item)

            # 保存到列表
            self.reminders.append(item)
            self._save_reminders()

            is_current_session = (unified_msg_origin == event.unified_msg_origin)
            if is_current_session:
                target_desc = "当前会话"
            else:
                # 提取群号显示
                if ':GroupMessage:' in unified_msg_origin:
                    group_id = unified_msg_origin.split(':GroupMessage:')[1]
                    target_desc = f"群聊 {group_id}"
                else:
                    target_desc = unified_msg_origin

            if is_task:
                result_msg = f"✅ {command_name}已添加！\n名称: {name}\n目标: {target_desc}\ncron: {cron_expr}\n指令: {content_text}"
            else:
                # 统计消息内容
                text_count = sum(1 for item in message_structure if item['type'] == 'text')
                image_count = sum(1 for item in message_structure if item['type'] == 'image')

                result_msg = f"✅ {command_name}已添加！\n名称: {name}\n目标: {target_desc}\ncron: {cron_expr}"
                if text_count > 0:
                    result_msg += f"\n文字: {text_count}段"
                if image_count > 0:
                    result_msg += f"\n图片: {image_count}张"

            logger.info(f"成功添加{command_name}: {name}, unified_msg_origin: {unified_msg_origin}, cron: {cron_expr}")
            yield event.plain_result(result_msg)

        except Exception as e:
            logger.error(f"添加{command_name}失败: {e}", exc_info=True)
            yield event.plain_result(f"添加{command_name}失败: {e}")

    @filter.command("添加任务")
    async def add_task(self, event: AstrMessageEvent):
        """添加定时任务
        格式1（当前会话）: /添加任务 <任务名称> <cron表达式> <指令>
        格式2（指定群号）: /添加任务 <任务名称> @<群号> <cron表达式> <指令>
        示例: /添加任务 每日签到 0 9 * * * /签到
        """
        async for result in self._add_task_or_reminder(event, is_task=True):
            yield result

    @filter.command("添加提醒")
    async def add_reminder(self, event: AstrMessageEvent):
        """添加定时提醒
        格式1（当前会话）: /添加提醒 <提醒名称> <cron表达式> <消息内容> [图片]
        格式2（指定群号）: /添加提醒 <提醒名称> @<群号> <cron表达式> <消息内容> [图片]
        示例: /添加提醒 每日提醒 0 9 * * * 早上好！[并附上图片]
        """
        async for result in self._add_task_or_reminder(event, is_task=False):
            yield result

    async def _list_items(self, event: AstrMessageEvent, name: str = "", show_tasks: bool = False):
        """查看提醒或任务的通用方法"""
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return

        if not self.reminders:
            yield event.plain_result("当前没有任务/提醒")
            return

        # 筛选对应类型的任务
        items = [item for item in self.reminders if item.get('is_task', False) == show_tasks]

        if not items:
            item_type = "任务" if show_tasks else "提醒"
            yield event.plain_result(f"当前没有{item_type}")
            return

        # 解析参数：检查是否指定了名称
        params = name.strip()

        if params:
            # 查看指定项的详细信息
            item_name = params
            target_item = None

            # 查找匹配的项
            for item in items:
                if item['name'] == item_name:
                    target_item = item
                    break

            if not target_item:
                item_type = "任务" if show_tasks else "提醒"
                yield event.plain_result(f"❌ 未找到名为 '{item_name}' 的{item_type}\n\n💡 使用 /查看{'任务' if show_tasks else '提醒'} 查看所有{item_type}列表")
                return

            # 构建消息链：添加基本属性信息
            chain = []

            # 格式化目标显示
            target = target_item.get('unified_msg_origin', '未知')
            item_type = "任务" if target_item.get('is_task', False) else "提醒"
            info_text = f"📋 {item_type}详情: {target_item['name']}\n\n"

            if ':GroupMessage:' in target:
                group_id = target.split(':GroupMessage:')[1]
                info_text += f"🎯 发送目标: 群聊 {group_id}\n"
            elif ':FriendMessage:' in target:
                friend_id = target.split(':FriendMessage:')[1]
                info_text += f"🎯 发送目标: 私聊 {friend_id}\n"
            else:
                info_text += f"🎯 发送目标: {target}\n"

            info_text += f"⏰ 定时规则: {target_item['cron']}\n"
            info_text += f"📅 创建时间: {target_item['created_at']}\n"
            info_text += f"👤 创建者ID: {target_item.get('created_by', '未知')}\n"

            if target_item.get('is_task', False):
                # 任务显示指令
                info_text += f"\n🔧 执行指令:\n{target_item.get('command', 'N/A')}\n"
            else:
                # 提醒显示内容
                info_text += f"\n📝 提醒内容:\n"

            chain.append(Plain(info_text))

            # 按照原始顺序构建内容
            if not target_item.get('is_task', False):
                # 只有提醒才显示消息结构
                # 显示提醒内容
                for item in target_item['message_structure']:
                    if item['type'] == 'text':
                        chain.append(Plain(item['content']))
                    elif item['type'] == 'image':
                        full_path = os.path.join(self.data_dir, item['path'])
                        if os.path.exists(full_path):
                            chain.append(Image.fromFileSystem(full_path))
                        else:
                            logger.warning(f"图片文件不存在: {full_path}")

            # 使用 MessageChain 返回
            message_chain = MessageChain()
            message_chain.chain = chain
            yield event.chain_result(message_chain.chain)

            # 如果是提醒且存在链接的任务，则单独发送链接任务信息
            if not target_item.get('is_task', False):
                reminder_name = target_item['name']
                if reminder_name in self.linked_tasks and self.linked_tasks[reminder_name]:
                    linked_commands = self.linked_tasks[reminder_name]
                    linked_info = f"🔗 {target_item['name']} 已链接的任务:\n"
                    for i, cmd in enumerate(linked_commands, 1):
                        linked_info += f"  {i}. {cmd}\n"
                    yield event.plain_result(linked_info)

        else:
            # 显示所有项列表（简略信息）
            item_type = "任务" if show_tasks else "提醒"
            result = f"📋 当前{item_type}列表:\n\n"
            for idx, item in enumerate(items, 1):
                result += f"{idx}. {item['name']}\n"

                # 格式化目标显示
                target = item.get('unified_msg_origin', '未知')
                if ':GroupMessage:' in target:
                    group_id = target.split(':GroupMessage:')[1]
                    result += f"   目标: 群聊 {group_id}\n"
                elif ':FriendMessage:' in target:
                    friend_id = target.split(':FriendMessage:')[1]
                    result += f"   目标: 私聊 {friend_id}\n"
                else:
                    result += f"   目标: {target}\n"

                result += f"   cron: {item['cron']}\n"

                if item.get('is_task', False):
                    # 任务显示指令
                    result += f"   指令: {item.get('command', 'N/A')}\n"
                else:
                    # 提醒显示内容统计
                    text_count = sum(1 for msg_item in item['message_structure'] if msg_item['type'] == 'text')
                    image_count = sum(1 for msg_item in item['message_structure'] if msg_item['type'] == 'image')

                    content_parts = []
                    if text_count > 0:
                        content_parts.append(f"文字{text_count}段")
                    if image_count > 0:
                        content_parts.append(f"图片{image_count}张")

                    if content_parts:
                        result += f"   内容: {' + '.join(content_parts)}\n"

                    # 显示链接的任务数量
                    reminder_name = item['name']
                    if reminder_name in self.linked_tasks and self.linked_tasks[reminder_name]:
                        linked_count = len(self.linked_tasks[reminder_name])
                        result += f"   🔗 链接任务: {linked_count}个\n"

                result += f"   创建时间: {item['created_at']}\n\n"

            result += f"💡 使用 /查看{'任务' if show_tasks else '提醒'} <{'任务' if show_tasks else '提醒'}名称> 查看详细内容"

            yield event.plain_result(result)

    @filter.command("查看任务")
    async def list_tasks(self, event: AstrMessageEvent, name: str = ""):
        """查看定时任务
        用法1: /查看任务 - 查看所有任务列表
        用法2: /查看任务 <任务名称> - 查看指定任务的详细信息
        """
        async for result in self._list_items(event, name, show_tasks=True):
            yield result

    @filter.command("查看提醒")
    async def list_reminders(self, event: AstrMessageEvent, name: str = ""):
        """查看提醒任务
        用法1: /查看提醒 - 查看所有提醒任务列表
        用法2: /查看提醒 <提醒名称> - 查看指定提醒的详细信息（包含完整文字和图片）
        """
        async for result in self._list_items(event, name, show_tasks=False):
            yield result

    async def _delete_item(self, event: AstrMessageEvent, index: int, delete_tasks: bool = False):
        """删除提醒或任务的通用方法"""
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return

        try:
            # 先检查是否有提醒任务
            if len(self.reminders) == 0:
                yield event.plain_result("❌ 当前没有任务/提醒")
                return

            # 筛选对应类型的任务
            items = [item for item in self.reminders if item.get('is_task', False) == delete_tasks]

            if not items:
                item_type = "任务" if delete_tasks else "提醒"
                yield event.plain_result(f"❌ 当前没有{item_type}")
                return

            if index < 1 or index > len(items):
                item_type = "任务" if delete_tasks else "提醒"
                yield event.plain_result(f"序号无效，请输入1-{len(items)}之间的数字")
                return

            # 获取要删除的项
            item_to_delete = items[index - 1]

            # 从调度器移除
            try:
                self.scheduler.remove_job(item_to_delete['id'])
            except Exception as e:
                logger.warning(f"从调度器移除任务失败: {e}")

            # 如果是提醒，删除关联的图片文件和链接的任务
            if not item_to_delete.get('is_task', False):
                # 删除关联的图片文件
                for msg_item in item_to_delete['message_structure']:
                    if msg_item['type'] == 'image':
                        img_path = os.path.join(self.data_dir, msg_item['path'])
                        try:
                            if os.path.exists(img_path):
                                os.remove(img_path)
                        except Exception as e:
                            logger.error(f"删除图片文件失败: {e}")

                # 删除关联的链接任务
                reminder_name = item_to_delete['name']
                if reminder_name in self.linked_tasks:
                    del self.linked_tasks[reminder_name]
                    logger.info(f"已删除提醒 '{reminder_name}' 的链接任务")

            # 从列表移除
            self.reminders.remove(item_to_delete)
            self._save_reminders()

            item_type = "任务" if delete_tasks else "提醒"
            yield event.plain_result(f"✅ 已删除{item_type}: {item_to_delete['name']}")

        except Exception as e:
            logger.error(f"删除{delete_tasks if '任务' else '提醒'}失败: {e}")
            yield event.plain_result(f"删除{delete_tasks if '任务' else '提醒'}失败: {e}")

    @filter.command("删除任务")
    async def delete_task(self, event: AstrMessageEvent, index: int = None):
        """删除定时任务
        用法: /删除任务 <序号>
        """
        if index is None:
            yield event.plain_result("❌ 参数缺失！\n用法: /删除任务 <序号>")
            return
        async for result in self._delete_item(event, index, delete_tasks=True):
            yield result

    @filter.command("链接提醒")
    async def link_reminder_to_task(self, event: AstrMessageEvent):
        """链接提醒到任务，提醒执行后执行指定指令
        格式: /链接提醒 <提醒名称> <指令> [参数可选]
        示例: /链接提醒 早安 /签到
        """
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return

        try:
            # 解析参数 - 移除任务名称参数，现在只需要提醒名称和指令
            parts = event.message_str.strip().split(' ', 2)
            if len(parts) < 3:
                yield event.plain_result(
                    "格式错误！\n"
                    "用法: /链接提醒 <提醒名称> <指令> [参数可选]\n"
                    "示例: /链接提醒 早安 /签到\n"
                    "说明: 当提醒「早安」执行后，会自动执行指令「/签到」\n"
                    "💡 支持为同一个提醒链接多个指令，将按添加顺序依次执行"
                )
                return

            _, reminder_name, command_with_args = parts

            # 验证提醒是否存在
            reminder_exists = False
            for item in self.reminders:
                if item['name'] == reminder_name and not item.get('is_task', False):
                    reminder_exists = True
                    break

            if not reminder_exists:
                yield event.plain_result(f"❌ 未找到名为 '{reminder_name}' 的提醒")
                return

            # 验证指令格式
            if not command_with_args:
                yield event.plain_result("❌ 指令不能为空")
                return

            # 检查是否已经存在相同的链接
            if reminder_name not in self.linked_tasks:
                self.linked_tasks[reminder_name] = []

            # 添加链接关系到列表 (现在允许重复链接)
            self.linked_tasks[reminder_name].append(command_with_args)
            self._save_reminders()

            # 计算当前链接的任务数量
            task_count = len(self.linked_tasks[reminder_name])
            yield event.plain_result(f"✅ 已将提醒 '{reminder_name}' 链接到指令: {command_with_args}\n当提醒执行后，将自动执行该指令。\n当前已链接 {task_count} 个指令。")

        except Exception as e:
            logger.error(f"链接提醒失败: {e}", exc_info=True)
            yield event.plain_result(f"链接提醒失败: {e}")

    @filter.command("删除提醒")
    async def delete_reminder(self, event: AstrMessageEvent, index: int = None):
        """删除提醒任务
        用法: /删除提醒 <序号>
        """
        if index is None:
            yield event.plain_result("❌ 参数缺失！\n用法: /删除提醒 <序号>")
            return
        async for result in self._delete_item(event, index, delete_tasks=False):
            yield result

    @filter.command("查看链接")
    async def list_linked_tasks(self, event: AstrMessageEvent):
        """查看所有链接的任务
        用法: /查看链接
        """
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return

        if not self.linked_tasks:
            yield event.plain_result("当前没有链接的任务")
            return

        result = "📋 当前链接任务列表:\n\n"
        count = 0
        for reminder_name, commands in self.linked_tasks.items():
            if commands:  # 确保有链接的命令
                count += 1
                result += f"{count}. 提醒 '{reminder_name}' 链接了 {len(commands)} 个任务:\n"
                for i, cmd in enumerate(commands, 1):
                    result += f"   {i}. {cmd}\n"
                result += "\n"

        if count == 0:
            yield event.plain_result("当前没有链接的任务")
            return

        result += "💡 使用 /链接提醒 <提醒名称> <指令> 来链接新任务\n"
        result += "💡 链接任务会在对应提醒执行后自动执行"
        yield event.plain_result(result)

    @filter.command("删除链接")
    async def delete_linked_task(self, event: AstrMessageEvent, reminder_index: int = None, command_index: int = None):
        """删除指定的链接任务
        用法: /删除链接 <提醒序号> <命令序号>
        示例: /删除链接 1 1 (删除第1个有链接的提醒的第1个链接命令)
        """
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return

        if reminder_index is None or command_index is None:
            yield event.plain_result("❌ 参数缺失！\n用法: /删除链接 <提醒序号> <命令序号>\n示例: /删除链接 1 1")
            return

        if not self.linked_tasks:
            yield event.plain_result("当前没有链接的任务")
            return

        # 获取所有有链接的提醒名称
        linked_reminders = []
        for reminder_name, commands in self.linked_tasks.items():
            if commands:
                linked_reminders.append(reminder_name)

        if not linked_reminders:
            yield event.plain_result("当前没有链接的任务")
            return

        if reminder_index < 1 or reminder_index > len(linked_reminders):
            yield event.plain_result(f"❌ 提醒序号无效！请输入 1-{len(linked_reminders)} 之间的数字")
            return

        selected_reminder = linked_reminders[reminder_index - 1]
        commands = self.linked_tasks[selected_reminder]

        if command_index < 1 or command_index > len(commands):
            yield event.plain_result(f"❌ 命令序号无效！该提醒有 {len(commands)} 个链接命令，请输入 1-{len(commands)} 之间的数字")
            return

        # 获取要删除的命令
        deleted_command = commands[command_index - 1]

        # 从列表中删除命令
        commands.pop(command_index - 1)

        # 如果该提醒没有更多链接命令了，删除该提醒的条目
        if not commands:
            del self.linked_tasks[selected_reminder]

        self._save_reminders()

        yield event.plain_result(f"✅ 已删除提醒 '{selected_reminder}' 的链接命令: {deleted_command}\n"
                               f"该提醒当前还有 {len(commands) if selected_reminder in self.linked_tasks else 0} 个链接命令")



    @filter.command("提醒帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """📖 定时提醒插件使用帮助

🔹 添加提醒
用法1（当前会话）: /添加提醒 <名称> <cron表达式> <消息>
用法2（指定群聊）: /添加提醒 <名称> @<群号> <cron表达式> <消息>
- cron表达式: 5段格式 (分 时 日 月 周)
- 💡 不指定群号时，自动发送到当前会话
- 💡 指定群号时，只能指定群聊，不支持私聊
- 💡 发送指令时可以同时附上图片，提醒会包含文字+图片
- 🔒 仅限Bot管理员使用

基础示例:
/添加提醒 早安 0 9 * * * 早上好！
(每天9点在当前会话发送)

/添加提醒 周报 @123456789 0 18 * * 5 本周工作总结
(每周五18点在指定群聊发送，可实现远程控制)

⭐ 多时间点示例（用逗号分隔）:
/添加提醒 喝水 0 9,14,18 * * * 记得喝水！
(每天9点、14点、18点各发送一次)

/添加提醒 课间休息 0 10,15,20 * * 1-5 该休息了
(工作日10点、15点、20点发送)

/添加提醒 整点报时 0 */2 * * * 当前时间...
(每2小时发送)

🔹 cron表达式详解
格式: 分 时 日 月 周
- 分: 0-59
- 时: 0-23
- 日: 1-31
- 月: 1-12
- 周: 0-6 (0=周日)

特殊符号:
- *: 任意值
- */n: 每n个单位
- a,b,c: 多个具体值（逗号分隔）
- a-b: 范围值

常用示例:
0 9 * * * - 每天9点
0 */2 * * * - 每2小时
30 18 * * 1-5 - 工作日18:30
0 0 1 * * - 每月1号0点
0 9,12,18 * * * - 每天9点、12点、18点
30 8-17/2 * * * - 8:30到17:30之间，每2小时

🔹 添加任务
用法1（当前会话）: /添加任务 <名称> <cron表达式> <指令>
用法2（指定群聊）: /添加任务 <名称> @<群号> <cron表达式> <指令>
- cron表达式: 5段格式 (分 时 日 月 周)
- 指令: 以指令前缀（如/）开头的指令，允许空格接参数
- 💡 不指定群号时，自动发送到当前会话
- 💡 指定群号时，只能指定群聊，不支持私聊
- 🔒 仅限Bot管理员使用

基础示例:
/添加任务 每日签到 0 9 * * * /签到
(每天9点在当前会话执行签到指令)

/添加任务 群签到 @123456789 0 9 * * * /签到
(每天9点在指定群聊执行签到指令)

🔹 查看提醒
/查看提醒 - 查看所有提醒任务列表
/查看提醒 <提醒名称> - 查看指定提醒的详细内容（包含完整文字和图片）

🔹 查看任务
/查看任务 - 查看所有任务列表
/查看任务 <任务名称> - 查看指定任务的详细信息

🔹 删除提醒
/删除提醒 <序号>

🔹 删除任务
/删除任务 <序号>

🔹 链接提醒
/链接提醒 <提醒名称> <指令> [参数可选]
- 说明: 当指定的提醒执行后，会自动执行指定的指令
- 示例: /链接提醒 早安 /签到
- 💡 指令需以指令前缀（如/）开头
- 💡 支持为同一个提醒链接多个指令，将按添加顺序依次执行
- 💡 现在允许同一个指令链接多次

🔹 查看链接
/查看链接
- 说明: 查看所有已链接的任务
- 用途: 管理和查看当前所有的链接任务关系

🔹 删除链接
/删除链接 <提醒序号> <命令序号>
- 说明: 删除指定的链接任务
- 示例: /删除链接 1 1 (删除第1个有链接的提醒的第1个链接命令)
- 用途: 精确管理链接任务，删除不需要的链接

🔹 帮助
/提醒帮助
"""
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时强制清理所有任务"""
        # 1. 关闭调度器
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            
        # 2. 强制取消所有正在运行的 CommandTrigger 监控任务
        if self._running_triggers:
            logger.info(f"正在清理 {len(self._running_triggers)} 个指令监控任务...")
            for task in self._running_triggers:
                if not task.done():
                    task.cancel()
            
            # 给 1 秒时间等待它们完成清理逻辑
            await asyncio.gather(*self._running_triggers, return_exceptions=True)
            self._running_triggers.clear()

        logger.info("定时提醒插件已彻底卸载并清理任务")


