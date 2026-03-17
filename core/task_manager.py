"""
任务管理器 - 处理定时任务相关的所有业务逻辑
"""

from typing import Dict, List, Optional, AsyncGenerator
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from apscheduler.triggers.cron import CronTrigger
from astrbot.api.message_components import At

from .utils import (
    build_job_id,
    collect_text_from_message_structure,
    describe_origin,
    extract_inline_message_structure,
    fetch_reply_message_structure,
    normalize_message_structure,
    translate_to_apscheduler_cron,
)
from .event_factory import EventFactory


class TaskManager:
    """任务管理器类"""

    def __init__(self, plugin):
        self.plugin = plugin

    async def add_task(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """添加定时任务"""
        try:
            from .utils import resolve_session_origin
            from astrbot.core.platform.astrbot_message import MessageType
            import re

            parts = event.message_str.strip().split(maxsplit=2)
            if len(parts) < 3:
                yield "❌ 参数缺失！用法: /添加任务 <任务名称> <cron表达式> <指令>"
                return

            _, name, remaining = parts

            # 名称合法性检查
            if re.fullmatch(r"\d+", name):
                yield "❌ 任务名称不能为纯阿拉伯数字"
                return

            # 尝试解析是否包含目标会话（@好友号 或 #群号）
            remaining_parts = remaining.split(maxsplit=1)
            session_param = None
            if len(remaining_parts) >= 2 and remaining_parts[0].startswith(('@', '#')):
                session_param = remaining_parts[0]
                remaining = remaining_parts[1]

            target_origin = resolve_session_origin(
                event.unified_msg_origin,
                event.get_message_type() == MessageType.GROUP_MESSAGE,
                session_param,
            )

            if not target_origin:
                yield "❌ 无法识别当前平台信息，请使用当前会话模式"
                return

            # 解析cron和指令
            cron_expr, command = self._parse_cron_and_command(remaining)

            if not cron_expr:
                yield "cron表达式格式错误！需要5段: 分 时 日 月 周"
                return

            # 验证cron表达式
            try:
                CronTrigger.from_crontab(translate_to_apscheduler_cron(cron_expr))
            except Exception as e:
                yield f"cron表达式无效: {e}"
                return

            # 检查重名
            for existing_item in self.plugin.reminders:
                if existing_item.get('name') == name and existing_item.get('is_task', False):
                    yield f"❌ 已存在名为 '{name}' 的任务，请使用其他名称"
                    return

            # 构建任务数据
            item = {
                'name': name,
                'is_task': True,
                'cron_expr': cron_expr,
                'enabled_sessions': [target_origin],
                'created_by': event.get_sender_id(),
                'creator_name': event.get_sender_name(),
                'is_admin': event.is_admin(),
                'self_id': event.get_self_id(),
            }

            # 处理指令内容
            await self._process_command_content(event, item, command, cron_expr)

            # 添加到列表
            self.plugin.reminders.append(item)

            # 调度任务
            await self.plugin._schedule_reminder(item)

            # 保存数据
            self.plugin._save_reminders()

            yield f"✅ 任务 '{name}' 添加成功！\n📅 Cron: {cron_expr}\n📍 目标: {describe_origin(target_origin)}"

        except Exception as e:
            logger.error(f"添加任务失败: {e}", exc_info=True)
            yield f"添加任务失败: {e}"

    async def edit_task(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """编辑定时任务"""
        try:
            from .utils import resolve_session_origin, describe_origin

            parts = event.message_str.strip().split(maxsplit=2)
            if len(parts) < 2:
                yield "❌ 参数缺失！用法: /编辑任务 <任务名称或序号> [@好友号|#群号] [cron表达式] [指令]\n提示: 所有参数都可选，可单独或组合编辑"
                return

            identifier, remaining = parts[1], parts[2] if len(parts) > 2 else ""

            # 查找目标任务（支持名称或序号）
            target_item = None
            if identifier.isdigit():
                # 使用序号查找
                idx = int(identifier) - 1
                tasks_only = [item for item in self.plugin.reminders if item.get('is_task', False)]
                if 0 <= idx < len(tasks_only):
                    target_item = tasks_only[idx]
                else:
                    yield f"❌ 序号无效，请输入1-{len(tasks_only)}之间的数字"
                    return
            else:
                # 使用名称查找
                name = identifier
                for item in self.plugin.reminders:
                    if item.get('is_task', False) and item.get('name') == name:
                        target_item = item
                        break

            if not target_item:
                yield f"❌ 未找到名为 '{identifier}' 的任务"
                return

            # 如果没有提供剩余参数，显示当前配置
            if not remaining.strip():
                current_cron = target_item.get('cron_expr', target_item.get('cron', '未设置'))
                current_command = target_item.get('command', '未设置')

                enabled_sessions = target_item.get('enabled_sessions', [])
                sessions_info = ""
                if enabled_sessions:
                    sessions_info = "\n📍 当前会话:\n" + "\n".join([f"  - {describe_origin(s)}" for s in enabled_sessions])
                else:
                    sessions_info = "\n📍 当前会话: 无"

                yield f"📋 当前任务配置：\nCron: {current_cron}\n指令: {current_command}{sessions_info}"
                return

            # 检查是否包含会话参数
            from astrbot.core.platform.astrbot_message import MessageType
            session_params = []
            remaining_parts = remaining.strip().split()
            filtered_remaining = []

            for part in remaining_parts:
                if part.startswith('@') or part.startswith('#'):
                    session_params.append(part)
                else:
                    filtered_remaining.append(part)

            # 如果有会话参数，解析并更新会话
            session_changed = False
            if session_params:
                new_sessions = []
                for target_session in session_params:
                    resolved = resolve_session_origin(
                        event.unified_msg_origin,
                        event.get_message_type() == MessageType.GROUP_MESSAGE,
                        target_session,
                    )
                    if resolved:
                        new_sessions.append(resolved)

                if new_sessions:
                    # 覆盖式更新会话
                    target_item['enabled_sessions'] = new_sessions
                    session_changed = True

                    # 重新调度
                    await self.plugin._reschedule_reminder(target_item)

            # 重新组合剩余参数用于解析cron和指令
            remaining = ' '.join(filtered_remaining)

            # 解析参数
            cron_expr, command = self._parse_edit_params(remaining)

            # 如果都没有提供，显示帮助
            if not cron_expr and not command and not session_params:
                yield "❌ 请提供要修改的cron表达式、指令或会话！\n用法: /编辑任务 <任务名称或序号> [@好友号|#群号] [cron表达式] [指令]"
                return

            # 更新cron表达式
            if cron_expr:
                target_item['cron_expr'] = cron_expr
                await self.plugin._reschedule_reminder(target_item)

            # 更新指令
            if command:
                await self._process_command_content(event, target_item, command, cron_expr or target_item.get('cron_expr', ''))

            # 保存更新
            self.plugin._save_reminders()

            updates = []
            if session_changed:
                new_sessions = target_item.get('enabled_sessions', [])
                sessions_str = ", ".join([describe_origin(s) for s in new_sessions])
                updates.append(f"✅ 会话已更新: {sessions_str}")
            if cron_expr:
                updates.append(f"✅ Cron表达式已更新: {cron_expr}")
            if command:
                updates.append(f"✅ 指令已更新")

            yield f"🎉 任务 '{target_item['name']}' 编辑成功！\n" + "\n".join(updates)

        except Exception as e:
            logger.error(f"编辑任务时出错: {e}", exc_info=True)
            yield f"❌ 编辑任务时出错: {e}"

    async def delete_task(self, event: AstrMessageEvent, key: str = None) -> AsyncGenerator[str, None]:
        """删除定时任务"""
        try:
            if not key:
                yield "❌ 参数缺失！用法: /删除任务 <任务名称或序号>"
                return

            # 查找目标任务
            target_index = None
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(self.plugin.reminders):
                    item = self.plugin.reminders[idx]
                    if item.get('is_task', False):
                        target_index = idx
            else:
                for i, item in enumerate(self.plugin.reminders):
                    if item.get('is_task', False) and item.get('name') == key:
                        target_index = i
                        break

            if target_index is None:
                yield f"❌ 未找到任务: {key}"
                return

            # 移除调度
            target_item = self.plugin.reminders[target_index]
            job_id = build_job_id(target_item['name'], True)
            if job_id in self.plugin.scheduler.get_jobs():
                self.plugin.scheduler.remove_job(job_id)

            # 从列表中移除
            self.plugin.reminders.pop(target_index)

            # 保存数据
            self.plugin._save_reminders()

            yield f"✅ 任务 '{target_item['name']}' 已删除"

        except Exception as e:
            logger.error(f"删除任务时出错: {e}", exc_info=True)
            yield f"❌ 删除任务时出错: {e}"

    async def list_tasks(self, event: AstrMessageEvent, name: str = "") -> AsyncGenerator[str, None]:
        """查看任务"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            if not self.plugin.reminders:
                yield "当前没有任务/提醒"
                return

            # 筛选任务
            items = [item for item in self.plugin.reminders if item.get('is_task', False)]

            if not items:
                yield "当前没有任务"
                return

            # 解析参数：检查是否指定了名称或序号
            params = str(name).strip()

            if params:
                target_item = None

                if params.isdigit():
                    idx = int(params)
                    if idx < 1 or idx > len(items):
                        yield f"❌ 序号无效，请输入1-{len(items)}之间的数字"
                        return
                    target_item = items[idx - 1]
                else:
                    item_name = params
                    for item in items:
                        if item['name'] == item_name:
                            target_item = item
                            break

                if not target_item:
                    yield f"❌ 未找到名为 '{params}' 的任务\n\n💡 使用 /查看任务 查看所有任务列表"
                    return

                # 构建详情消息
                enabled_sessions = target_item.get('enabled_sessions', [])
                info_text = f"📋 任务详情: {target_item['name']}\n\n"
                if enabled_sessions:
                    info_text += "🎯 已启用会话:\n"
                    for s in enabled_sessions:
                        info_text += f"{describe_origin(s)}\n"
                else:
                    info_text += "🎯 当前未在任何会话启用\n"

                info_text += f"⏰ 定时规则: {target_item.get('cron', target_item.get('cron_expr', 'N/A'))}\n"
                info_text += f"📅 创建时间: {target_item.get('created_at', 'N/A')}\n"
                info_text += f"👤 创建者ID: {target_item.get('created_by', '未知')}\n"
                info_text += f"\n🔧 执行指令:\n{target_item.get('command', 'N/A')}\n"

                yield info_text
            else:
                # 显示所有任务列表（简略信息）
                result = f"📋 当前任务列表:\n\n"
                for idx, item in enumerate(items, 1):
                    result += f"{idx}. {item['name']}\n"

                yield result

        except Exception as e:
            logger.error(f"查看任务时出错: {e}", exc_info=True)
            yield f"❌ 查看任务时出错: {e}"

    async def toggle_task_session(self, event: AstrMessageEvent, enable: bool) -> AsyncGenerator[str, None]:
        """启动或停止任务"""
        try:
            from .utils import resolve_session_origin
            from astrbot.core.platform.astrbot_message import MessageType
            from typing import List

            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            parts = event.message_str.strip().split()
            action = "启动" if enable else "停止"

            if len(parts) < 2:
                yield f"❌ 参数缺失！\n用法: /{action}任务 <任务名> [@好友号|#群号 ...]"
                return

            name = parts[1]
            session_params = parts[2:]

            if session_params:
                invalid_params = [p for p in session_params if not p.startswith('@') and not p.startswith('#')]
                if invalid_params:
                    invalid_str = " ".join(invalid_params)
                    yield f"❌ 会话参数无效: {invalid_str}\n用法: /{action}任务 <任务名> [@好友号|#群号 ...]"
                    return
                raw_targets = session_params
            else:
                raw_targets = [None]

            target_item = None
            for item in self.plugin.reminders:
                if item.get('is_task', False) and item.get('name') == name:
                    target_item = item
                    break

            if not target_item:
                yield f"❌ 未找到名为 '{name}' 的任务"
                return

            resolved_sessions: List[str] = []
            unresolved_targets: List[str] = []
            seen_sessions = set()

            for raw_target in raw_targets:
                resolved = resolve_session_origin(
                    event.unified_msg_origin,
                    event.get_message_type() == MessageType.GROUP_MESSAGE,
                    raw_target,
                )
                if not resolved:
                    unresolved_targets.append(raw_target or "当前会话")
                    continue
                if resolved in seen_sessions:
                    continue
                seen_sessions.add(resolved)
                resolved_sessions.append(resolved)

            if not resolved_sessions:
                unresolved_str = " ".join(unresolved_targets) if unresolved_targets else "当前会话"
                yield f"❌ 无法识别目标会话: {unresolved_str}\n请检查会话参数（@好友号 或 #群号）"
                return

            enabled_sessions = list(target_item.get('enabled_sessions', []))
            changed = False
            success_sessions: List[str] = []
            skipped_sessions: List[str] = []

            for session in resolved_sessions:
                if enable:
                    if session in enabled_sessions:
                        skipped_sessions.append(session)
                        continue
                    enabled_sessions.append(session)
                    self.plugin._add_job(target_item, session)
                    success_sessions.append(session)
                    changed = True
                else:
                    if session not in enabled_sessions:
                        skipped_sessions.append(session)
                        continue
                    enabled_sessions.remove(session)
                    self.plugin._remove_job(target_item, session)
                    success_sessions.append(session)
                    changed = True

            target_item['enabled_sessions'] = enabled_sessions
            if changed:
                self.plugin._save_reminders()

            lines = [f"✅ {action}任务完成: {name}"]

            if success_sessions:
                lines.append(f"成功: {len(success_sessions)} 个会话")
                for session in success_sessions:
                    lines.append(f"- {describe_origin(session)}")

            if skipped_sessions:
                skipped_reason = "已启用" if enable else "未启用"
                lines.append(f"跳过: {len(skipped_sessions)} 个会话（{skipped_reason}）")
                for session in skipped_sessions:
                    lines.append(f"- {describe_origin(session)}")

            if unresolved_targets:
                lines.append(f"无法识别: {' '.join(unresolved_targets)}")

            yield "\n".join(lines)

        except Exception as e:
            logger.error(f"{action}任务时出错: {e}", exc_info=True)
            yield f"❌ {action}任务时出错: {e}"

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有权限使用该插件"""
        try:
            if event.is_admin():
                return True
            whitelist = self.plugin.config.get('whitelist', [])
            if not whitelist:
                return True
            return event.get_sender_id() in whitelist
        except Exception:
            return True

    async def execute_task(self, item: Dict, session: str):
        """执行定时任务"""
        command = item.get('command', '')
        if not command:
            logger.warning(f"任务 '{item.get('name')}' 没有指令，跳过执行")
            return

        original_components = item.get('message_structure', [])
        is_admin = item.get('is_admin', True)
        self_id = item.get('self_id')

        # 转换消息结构为组件
        component_list = []
        for comp_data in original_components:
            if comp_data.get('type') == 'at':
                component_list.append(At(qq=comp_data.get('qq')))

        logger.info(f"任务 '{item.get('name')}' 执行: {command}")

        # 直接创建事件并派发
        factory = EventFactory(self.plugin.context)
        event = factory.create_event(
            session,
            command,
            item.get('created_by', 'timer'),
            item.get('creator_name', 'Timer'),
            original_components=component_list,
            is_admin=is_admin,
            self_id=self_id
        )

        try:
            event.set_extra("reminder_timer_origin", True)
        except Exception:
            pass

        # 派发事件
        self.plugin.context.get_event_queue().put_nowait(event)
        logger.info(f"任务 '{item.get('name')}' 已派发: {command}")

    def _resolve_target_session(self, event: AstrMessageEvent, target_session: str) -> Optional[str]:
        """解析目标会话"""
        # 这里需要根据实际需求实现
        # 暂时返回当前会话
        return event.unified_msg_origin

    def _parse_cron_and_command(self, remaining: str) -> tuple:
        """解析cron表达式和指令"""
        remaining_parts = remaining.split(maxsplit=5)
        if len(remaining_parts) < 5:
            return None, remaining

        cron_parts = remaining_parts[:5]
        last_part = cron_parts[4]
        cleaned_last_part = self._clean_cron_part(last_part)

        if not cleaned_last_part:
            return None, remaining

        cron_parts[4] = cleaned_last_part
        cron_expr = ' '.join(cron_parts)

        # 提取指令
        command = ""
        if len(remaining_parts) > 5:
            command = remaining_parts[5]
        if len(last_part) > len(cleaned_last_part):
            command = last_part[len(cleaned_last_part):] + (' ' + command if command else '')

        return cron_expr, command.strip()

    def _parse_edit_params(self, remaining: str) -> tuple:
        """解析编辑参数"""
        remaining_parts = remaining.strip().split(maxsplit=5)

        # 尝试解析为cron表达式
        cron_expr = None
        if len(remaining_parts) >= 5:
            cron_parts = remaining_parts[:5]
            last_part = cron_parts[4]
            cleaned_last_part = self._clean_cron_part(last_part)

            if cleaned_last_part:
                cron_parts[4] = cleaned_last_part
                new_cron = ' '.join(cron_parts)

                try:
                    CronTrigger.from_crontab(translate_to_apscheduler_cron(new_cron))
                    cron_expr = new_cron
                except Exception:
                    pass

        # 获取指令部分
        command = None
        if len(remaining_parts) > 5:
            command = remaining_parts[5].strip()
        elif cron_expr is None:
            command = remaining.strip()
        else:
            last_part = remaining_parts[4]
            cleaned_last_part = self._clean_cron_part(last_part)
            if len(last_part) > len(cleaned_last_part):
                command = last_part[len(cleaned_last_part):].strip()
            if len(remaining_parts) > 5 and remaining_parts[5]:
                command = (command + " " + remaining_parts[5]).strip()

        return cron_expr, command

    def _clean_cron_part(self, part: str) -> str:
        """清理cron表达式的部分"""
        cleaned = ''
        for i, char in enumerate(part):
            if char.isalnum() or char in '*-,/':
                if char.isdigit():
                    digit_count = 1
                    for j in range(i + 1, min(i + 10, len(part))):
                        if part[j].isdigit():
                            digit_count += 1
                        else:
                            break
                    if digit_count > 3:
                        break
                cleaned += char
            else:
                break
        return cleaned

    async def _process_command_content(self, event, item: Dict, command: str, cron_expr: str):
        """处理指令内容"""
        message_structure = await extract_inline_message_structure(
            event.get_messages(),
            cron_expr,
            lambda comp: self.plugin._save_media_component(comp, getattr(comp, 'type', 'media').lower()),
        )
        message_structure = normalize_message_structure(message_structure)
        normalized_command = collect_text_from_message_structure(message_structure).strip()

        final_command = command if command else normalized_command
        if not final_command:
            raise ValueError("任务指令不能为空")

        item['command'] = final_command
        if message_structure:
            item['message_structure'] = message_structure

    async def execute_now(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """立即执行任务"""
        try:
            import re

            # 解析参数
            parts = event.message_str.strip().split(maxsplit=1)
            if len(parts) < 2:
                yield "❌ 参数缺失！用法: /执行任务 <任务名称或序号>"
                return

            key = parts[1].strip()

            # 查找任务
            item = None
            if re.fullmatch(r"\d+", key):
                # 按序号查找
                index = int(key) - 1
                task_list = [i for i in self.plugin.reminders if i.get('is_task', False)]
                if 0 <= index < len(task_list):
                    item = task_list[index]
            else:
                # 按名称查找
                for i in self.plugin.reminders:
                    if i.get('name') == key and i.get('is_task', False):
                        item = i
                        break

            if not item:
                yield f"❌ 未找到任务: {key}"
                return

            # 在所有启用的会话中执行
            enabled_sessions = item.get('enabled_sessions', [])
            if not enabled_sessions:
                yield f"❌ 任务 '{item.get('name')}' 未在任何会话启用"
                return

            for session in enabled_sessions:
                await self.execute_task(item, session)

            yield f"✅ 任务 '{item.get('name')}' 已在 {len(enabled_sessions)} 个会话中执行"

        except Exception as e:
            logger.error(f"执行任务失败: {e}", exc_info=True)
            yield f"❌ 执行任务失败: {e}"