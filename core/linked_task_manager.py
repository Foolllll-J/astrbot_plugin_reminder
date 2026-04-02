"""
链接任务管理器 - 处理提醒与任务的链接功能
"""

from typing import Dict, List, AsyncGenerator
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Face

from .utils import (
    collect_text_from_message_structure,
    normalize_message_structure,
    is_user_allowed,
)


class LinkedTaskManager:
    """链接任务管理器类"""

    def __init__(self, plugin):
        self.plugin = plugin

    def _find_reminder_by_identifier(self, identifier: str):
        """Resolve a reminder by display index or exact name."""
        reminder_items = [item for item in self.plugin.reminders if not item.get('is_task', False)]

        if identifier.isdigit():
            index = int(identifier) - 1
            if 0 <= index < len(reminder_items):
                return reminder_items[index]
            return None

        for item in reminder_items:
            if item.get('name') == identifier:
                return item
        return None

    async def link_reminder_to_task(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """将指令链接到提醒，提醒执行后自动执行对应指令
        用法: /链接提醒 <提醒名称或序号> <指令> [参数可选]
        示例: /链接提醒 早安 /签到
        说明: 当提醒「早安」执行后，会自动执行指令「/签到」
        💡 支持为同一个提醒链接多个指令，将按添加顺序依次执行
        """
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            # 解析参数
            parts = event.message_str.strip().split(' ', 2)
            if len(parts) < 3:
                yield (
                    "格式错误！\n"
                    "用法: /链接提醒 <提醒名称或序号> <指令> [参数可选]\n"
                    "示例: /链接提醒 早安 /签到\n"
                    "说明: 当提醒「早安」执行后，会自动执行指令「/签到」\n"
                    "💡 支持为同一个提醒链接多个指令，将按添加顺序依次执行"
                )
                return

            _, reminder_identifier, command_with_args = parts

            reminder_item = self._find_reminder_by_identifier(reminder_identifier)
            if not reminder_item:
                yield f"❌ 未找到名为 '{reminder_identifier}' 的提醒"
                return

            reminder_name = reminder_item['name']

            # 验证指令格式
            if not command_with_args:
                yield "❌ 指令不能为空"
                return

            # 初始化链接任务列表
            if reminder_name not in self.plugin.linked_tasks:
                self.plugin.linked_tasks[reminder_name] = []

            # 解析指令文本和消息组件
            parsed_command_structure = normalize_message_structure([{'type': 'text', 'content': command_with_args}])
            normalized_command = collect_text_from_message_structure(parsed_command_structure).strip()
            if normalized_command:
                command_with_args = normalized_command
            if not command_with_args:
                yield "❌ 解析后指令为空，请至少保留实际命令文本"
                return

            # 提取 At 和 Face 组件
            message_structure = []
            for comp in parsed_command_structure:
                if comp.get('type') in ('at', 'atall'):
                    message_structure.append(comp)

            for msg_comp in event.get_messages():
                if isinstance(msg_comp, At):
                    qq = str(msg_comp.qq)
                    if qq.lower() == 'all':
                        message_structure.append({
                            "type": "atall"
                        })
                    else:
                        message_structure.append({
                            "type": "at",
                            "qq": qq
                        })
                elif isinstance(msg_comp, Face):
                    message_structure.append({
                        "type": "face",
                        "id": msg_comp.id
                    })

            # 创建链接项
            linked_item = {
                'command': command_with_args,
                'message_structure': message_structure
            }
            self.plugin.linked_tasks[reminder_name].append(linked_item)
            self.plugin._save_reminders()

            # 计算当前链接的任务数量
            task_count = len(self.plugin.linked_tasks[reminder_name])
            yield (
                f"✅ 已将指令链接到提醒「{reminder_name}」: {command_with_args}\n"
                f"当该提醒执行时，将自动执行此指令。\n"
                f"当前该提醒已链接 {task_count} 个指令。"
            )

        except Exception as e:
            logger.error(f"链接提醒失败: {e}", exc_info=True)
            yield f"链接提醒失败: {e}"

    async def list_linked_tasks(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """查看已链接的任务
        用法1: /查看链接 - 显示所有提醒及其链接的任务
        用法2: /查看链接 <提醒名称或序号> - 显示指定提醒的链接任务详情
        """
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            # 解析参数
            parts = event.message_str.strip().split()
            reminder_identifier = parts[1] if len(parts) > 1 else None
            reminder_name = None

            if not self.plugin.linked_tasks or not self.plugin.linked_tasks:
                yield "当前没有链接的任务"
                return

            # 如果指定了提醒名称或序号，只显示该提醒的链接任务
            if reminder_identifier:
                reminder_item = self._find_reminder_by_identifier(reminder_identifier)
                if not reminder_item:
                    yield f"❌ 未找到名为 '{reminder_identifier}' 的提醒"
                    return

                reminder_name = reminder_item['name']
                if reminder_name not in self.plugin.linked_tasks or not self.plugin.linked_tasks[reminder_name]:
                    yield f"提醒 '{reminder_name}' 没有链接任何任务"
                    return

                linked_commands = self.plugin.linked_tasks[reminder_name]
                result = f"📋 提醒 '{reminder_name}' 已链接的任务:\n\n"
                for i, cmd_data in enumerate(linked_commands, 1):
                    cmd_str = cmd_data if isinstance(cmd_data, str) else cmd_data.get('command', '')
                    # 显示组件信息
                    extras = []
                    if isinstance(cmd_data, dict):
                        msg_struct = cmd_data.get('message_structure', [])
                        face_count = sum(1 for x in msg_struct if x['type'] == 'face')
                        at_count = sum(1 for x in msg_struct if x['type'] == 'at')
                        atall_count = sum(1 for x in msg_struct if x['type'] == 'atall')
                        if face_count:
                            extras.append(f"表情{face_count}个")
                        if at_count:
                            extras.append(f"At{at_count}人")
                        if atall_count:
                            extras.append(f"@全体{atall_count}次")
                    extra_str = f" ({' + '.join(extras)})" if extras else ""
                    result += f"{i}. {cmd_str}{extra_str}\n"

                yield result
            else:
                # 显示所有提醒的链接任务
                result = "📋 当前链接任务列表:\n\n"
                count = 0
                for reminder_name, commands in self.plugin.linked_tasks.items():
                    if commands:  # 确保有链接的命令
                        count += 1
                        result += f"{count}. 提醒 '{reminder_name}' 链接了 {len(commands)} 个任务:\n"
                        for i, cmd_data in enumerate(commands, 1):
                            cmd_str = ""
                            extras = []
                            if isinstance(cmd_data, str):
                                cmd_str = cmd_data
                            elif isinstance(cmd_data, dict):
                                cmd_str = cmd_data.get('command', '')
                                msg_struct = cmd_data.get('message_structure', [])
                                face_count = sum(1 for x in msg_struct if x['type'] == 'face')
                                at_count = sum(1 for x in msg_struct if x['type'] == 'at')
                                atall_count = sum(1 for x in msg_struct if x['type'] == 'atall')
                                if face_count:
                                    extras.append(f"表情{face_count}个")
                                if at_count:
                                    extras.append(f"At{at_count}人")
                                if atall_count:
                                    extras.append(f"@全体{atall_count}次")

                            extra_str = f" ({' + '.join(extras)})" if extras else ""
                            result += f"   {i}. {cmd_str}{extra_str}\n"
                        result += "\n"

                if count == 0:
                    yield "当前没有链接的任务"
                    return

                result += "💡 使用 /链接提醒 <提醒名称或序号> <指令> 来链接新任务\n"
                result += "💡 链接任务会在对应提醒执行后自动执行"
                yield result

        except Exception as e:
            logger.error(f"查看链接任务失败: {e}", exc_info=True)
            yield f"查看链接任务失败: {e}"

    async def delete_linked_task(self, event: AstrMessageEvent, reminder_index: int = None, command_index: int = None) -> AsyncGenerator[str, None]:
        """删除已链接的任务
        用法1: /删除链接 - 交互式删除，显示所有链接任务列表
        用法2: /删除链接 <提醒序号> <任务序号> - 直接删除指定链接任务
        示例: /删除链接 1 1 (删除第1个有链接的提醒的第1个链接命令)
        """
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            if not self.plugin.linked_tasks or not self.plugin.linked_tasks:
                yield "当前没有链接的任务"
                return

            # 获取所有有链接任务的提醒
            linked_reminders = []
            for reminder_name, linked_commands in self.plugin.linked_tasks.items():
                if linked_commands:
                    linked_reminders.append(reminder_name)

            if not linked_reminders:
                yield "当前没有链接的任务"
                return

            # 如果通过参数指定了索引
            if reminder_index is not None:
                if command_index is None:
                    yield "❌ 请同时提供提醒序号和任务序号，或使用 /删除链接 进行交互式删除"
                    return

                reminder_index = int(reminder_index)
                command_index = int(command_index)

                if reminder_index < 1 or reminder_index > len(linked_reminders):
                    yield f"❌ 提醒序号无效！请输入 1-{len(linked_reminders)} 之间的数字"
                    return

                selected_reminder = linked_reminders[reminder_index - 1]
                commands = self.plugin.linked_tasks[selected_reminder]

                if command_index < 1 or command_index > len(commands):
                    yield f"❌ 命令序号无效！该提醒有 {len(commands)} 个链接命令，请输入 1-{len(commands)} 之间的数字"
                    return

                # 获取要删除的命令
                deleted_command_data = commands[command_index - 1]
                deleted_command = deleted_command_data if isinstance(deleted_command_data, str) else deleted_command_data.get(
                    'command', '')

                # 从列表中删除命令
                commands.pop(command_index - 1)

                # 如果该提醒没有更多链接命令了，删除该提醒的条目
                if not commands:
                    del self.plugin.linked_tasks[selected_reminder]

                self.plugin._save_reminders()
                yield f"✅ 已删除提醒 '{selected_reminder}' 的链接命令: {deleted_command}\n该提醒当前还有 {len(commands) if selected_reminder in self.plugin.linked_tasks else 0} 个链接命令"
                return

            # 交互式删除 - 显示所有链接任务列表
            result = "📋 当前链接任务列表:\n\n"
            count = 0
            for reminder_name, commands in self.plugin.linked_tasks.items():
                if commands:
                    count += 1
                    result += f"{count}. 提醒 '{reminder_name}' 链接了 {len(commands)} 个任务:\n"
                    for i, cmd_data in enumerate(commands, 1):
                        cmd_str = ""
                        extras = []
                        if isinstance(cmd_data, str):
                            cmd_str = cmd_data
                        elif isinstance(cmd_data, dict):
                            cmd_str = cmd_data.get('command', '')
                            msg_struct = cmd_data.get('message_structure', [])
                            face_count = sum(1 for x in msg_struct if x['type'] == 'face')
                            at_count = sum(1 for x in msg_struct if x['type'] == 'at')
                            atall_count = sum(1 for x in msg_struct if x['type'] == 'atall')
                            if face_count:
                                extras.append(f"表情{face_count}个")
                            if at_count:
                                extras.append(f"At{at_count}人")
                            if atall_count:
                                extras.append(f"@全体{atall_count}次")

                        extra_str = f" ({' + '.join(extras)})" if extras else ""
                        result += f"   {i}. {cmd_str}{extra_str}\n"
                    result += "\n"

            if count == 0:
                yield "当前没有链接的任务"
                return

            result += "💡 使用: /删除链接 <提醒序号> <任务序号> 来删除指定的链接任务"
            yield result

        except Exception as e:
            logger.error(f"删除链接任务失败: {e}", exc_info=True)
            yield f"删除链接任务失败: {e}"

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有权限使用该插件"""
        return is_user_allowed(self.plugin, event)
