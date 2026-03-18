"""
任务管理器 - 处理定时任务相关的所有业务逻辑
"""

import re
import datetime
from typing import Dict, List, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import MessageType
from astrbot.api.message_components import At, Face, Plain
from apscheduler.triggers.cron import CronTrigger

from .utils import (
    collect_text_from_message_structure,
    describe_origin,
    extract_inline_message_structure,
    normalize_session_param_token,
    normalize_message_structure,
    resolve_session_origin,
    translate_to_apscheduler_cron,
    is_user_allowed,
)
from .event_factory import EventFactory


class TaskManager:
    """任务管理器类"""

    def __init__(self, plugin):
        self.plugin = plugin

    async def add_task(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """添加定时任务"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

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
                normalized = normalize_session_param_token(remaining_parts[0])
                if normalized:
                    session_param = normalized
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
                'created_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            # 处理指令内容
            await self._process_command_content(event, item, command, cron_expr)

            # 添加到列表
            self.plugin.reminders.append(item)

            # 调度任务
            await self.plugin._schedule_reminder(item)

            # 保存数据
            self.plugin._save_reminders()

            yield f"✅ 任务 '{name}' 添加成功！\nCron: {cron_expr}\n目标: {describe_origin(target_origin)}"

        except Exception as e:
            logger.error(f"添加任务失败: {e}", exc_info=True)
            yield f"添加任务失败: {e}"

    async def edit_task(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """编辑定时任务"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            parts = event.message_str.strip().split(maxsplit=2)
            if len(parts) < 2:
                yield "❌ 参数缺失！用法: /编辑任务 <任务名称或序号> [@好友号|#群号] [cron表达式] [指令]\n提示: 所有参数都可选，可单独或组合编辑"
                return

            identifier, remaining = parts[1], parts[2] if len(parts) > 2 else ""
            tokens_all = self._extract_message_tokens(event)
            session_params, remaining_tokens = self._split_edit_remaining_tokens(tokens_all)

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
            if not remaining_tokens and not session_params:
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

            # 解析参数
            cron_expr, command = self._parse_edit_params_from_tokens(remaining_tokens)

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

            # 如果有任何变更，记录编辑时间并更新编辑者信息
            if session_changed or cron_expr or command:
                target_item['edited_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                target_item['created_by'] = event.get_sender_id()
                target_item['creator_name'] = event.get_sender_name()
                target_item['is_admin'] = event.is_admin()

            # 保存更新
            self.plugin._save_reminders()

            current_cron = target_item.get('cron_expr', target_item.get('cron', ''))
            sessions = list(target_item.get('enabled_sessions', []))
            session_count = len(sessions)
            msg = (
                "✅ 任务已编辑！\n"
                f"名称: {target_item.get('name', '')}\n"
                f"cron: {current_cron}\n"
                f"指令: {target_item.get('command', '')}\n"
                f"已影响会话数: {session_count}"
            )

            yield msg

        except Exception as e:
            logger.error(f"编辑任务时出错: {e}", exc_info=True)
            yield f"❌ 编辑任务时出错: {e}"

    async def delete_task(self, event: AstrMessageEvent, key: str = None) -> AsyncGenerator[str, None]:
        """删除定时任务"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            if not key:
                yield "❌ 参数缺失！用法: /删除任务 <任务名称或序号>"
                return

            # 获取过滤后的任务列表（只包含任务）
            task_items = [item for item in self.plugin.reminders if item.get('is_task', False)]

            # 查找目标任务
            target_index = None
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(task_items):
                    target_item = task_items[idx]
                    # 在原始列表中找到对应的索引
                    for i, item in enumerate(self.plugin.reminders):
                        if item['name'] == target_item['name']:
                            target_index = i
                            break
            else:
                for i, item in enumerate(task_items):
                    if item.get('name') == key:
                        # 在原始列表中找到对应的索引
                        for j, orig_item in enumerate(self.plugin.reminders):
                            if orig_item['name'] == key:
                                target_index = j
                                break
                        break

            if target_index is None:
                yield f"❌ 未找到任务: {key}"
                return

            # 移除调度
            target_item = self.plugin.reminders[target_index]
            self.plugin._remove_all_jobs_for_item(target_item)

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
                # 优先显示编辑时间，如果没有则显示创建时间
                if target_item.get('edited_at'):
                    info_text += f"📅 最后编辑: {target_item['edited_at']}\n"
                else:
                    info_text += f"📅 创建时间: {target_item.get('created_at', 'N/A')}\n"
                info_text += f"👤 创建者ID: {target_item.get('created_by', '未知')}\n"
                info_text += f"\n🔧 执行指令:\n{target_item.get('command', 'N/A')}\n"

                yield info_text
            else:
                # 显示所有任务列表（简略信息）
                result = "📋 当前任务列表:\n\n"
                for idx, item in enumerate(items, 1):
                    result += f"{idx}. {item['name']}\n"

                    enabled_sessions = item.get('enabled_sessions', [])
                    if enabled_sessions:
                        result += f"   已启用会话数: {len(enabled_sessions)}\n"
                    else:
                        result += "   已启用会话数: 0\n"

                    result += f"   cron: {item.get('cron_expr', item.get('cron', 'N/A'))}\n"
                    result += f"   指令: {item.get('command', 'N/A')}\n"
                    result += f"   创建时间: {item.get('created_at', 'N/A')}\n\n"

                result += "💡 使用 /查看任务 <序号或名称> 查看详细内容"
                yield result

        except Exception as e:
            logger.error(f"查看任务时出错: {e}", exc_info=True)
            yield f"❌ 查看任务时出错: {e}"

    async def toggle_task_session(self, event: AstrMessageEvent, enable: bool) -> AsyncGenerator[str, None]:
        """启动或停止任务"""
        try:
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
                invalid_params = [
                    p for p in session_params if normalize_session_param_token(p) is None
                ]
                if invalid_params:
                    invalid_str = " ".join(invalid_params)
                    yield f"❌ 会话参数无效: {invalid_str}\n用法: /{action}任务 <任务名> [@好友号|#群号 ...]"
                    return
                raw_targets = [normalize_session_param_token(p) for p in session_params if normalize_session_param_token(p)]
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
        return is_user_allowed(self.plugin, event)

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
            elif comp_data.get('type') == 'atall':
                component_list.append(At(qq="all"))
            elif comp_data.get('type') == 'face':
                component_list.append(Face(id=comp_data.get('id')))

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

    def _parse_edit_params_from_tokens(self, tokens: List[str]) -> tuple:
        """从 token 列表解析 cron 与指令。"""
        cron_expr = None
        command_start_idx = 0

        if len(tokens) >= 5:
            cron_candidate = tokens[:5]
            try:
                cleaned_last = self._clean_cron_part(cron_candidate[4])
                if cleaned_last:
                    cron_candidate[4] = cleaned_last
                    test_cron = ' '.join(cron_candidate)
                    CronTrigger.from_crontab(translate_to_apscheduler_cron(test_cron))
                    cron_expr = test_cron
                    command_start_idx = 5
            except Exception:
                pass

        if command_start_idx < len(tokens):
            command = ' '.join(tokens[command_start_idx:])
        else:
            command = None

        return cron_expr, command

    def _extract_message_tokens(self, event: AstrMessageEvent) -> List[str]:
        """从消息链提取文本 token（忽略非文本组件）。"""
        tokens: List[str] = []
        for msg_comp in event.get_messages():
            if isinstance(msg_comp, Plain):
                text = msg_comp.text or ""
                if text.strip():
                    tokens.extend(text.split())
        return tokens

    def _split_edit_remaining_tokens(self, tokens: List[str]) -> tuple[List[str], List[str]]:
        """剥离命令头与会话参数后的 token 列表。"""
        if len(tokens) < 2:
            return [], []

        remaining_tokens = tokens[2:]
        session_params: List[str] = []
        filtered_remaining: List[str] = []

        for part in remaining_tokens:
            if part.startswith('@') or part.startswith('#'):
                normalized = normalize_session_param_token(part)
                if normalized:
                    session_params.append(normalized)
                else:
                    filtered_remaining.append(part)
            else:
                filtered_remaining.append(part)

        return session_params, filtered_remaining

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
        """处理指令内容（新增和编辑都使用相同逻辑）"""
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
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

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
