"""
提醒管理器 - 处理定时提醒相关的所有业务逻辑
"""

from typing import Dict, List, Optional, AsyncGenerator
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from apscheduler.triggers.cron import CronTrigger
import os
import asyncio

from .utils import (
    build_job_id,
    describe_origin,
    extract_inline_message_structure,
    fetch_reply_message_structure,
    format_duration_cn,
    normalize_message_structure,
    parse_recall_seconds,
    translate_to_apscheduler_cron,
    build_message_chain_from_structure,
    split_message_structure,
    extract_message_id,
)


class ReminderManager:
    """提醒管理器类"""

    def __init__(self, plugin):
        self.plugin = plugin

    async def add_reminder(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """添加定时提醒"""
        try:
            from .utils import resolve_session_origin
            from astrbot.core.platform.astrbot_message import MessageType
            import re

            parts = event.message_str.strip().split(maxsplit=2)
            if len(parts) < 3:
                yield "❌ 参数缺失！用法: /添加提醒 <提醒名称> <cron表达式> <消息内容> [图片]"
                return

            _, name, remaining = parts

            # 名称合法性检查
            if re.fullmatch(r"\d+", name):
                yield "❌ 提醒名称不能为纯阿拉伯数字"
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

            # 解析cron和内容
            cron_expr, content_text = self._parse_cron_and_content(remaining)

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
                if existing_item.get('name') == name and not existing_item.get('is_task', False):
                    yield f"❌ 已存在名为 '{name}' 的提醒，请使用其他名称"
                    return

            # 构建提醒数据
            item = {
                'name': name,
                'is_task': False,
                'cron_expr': cron_expr,
                'enabled_sessions': [target_origin],
                'created_by': event.get_sender_id(),
                'creator_name': event.get_sender_name(),
                'is_admin': event.is_admin(),
                'self_id': event.get_self_id(),
            }

            # 处理提醒内容
            await self._process_reminder_content(event, item, content_text, cron_expr)

            # 添加到列表
            self.plugin.reminders.append(item)

            # 调度提醒
            await self.plugin._schedule_reminder(item)

            # 保存数据
            self.plugin._save_reminders()

            recall_info = ""
            if item.get('recall_after_seconds', 0) > 0:
                recall_info = f"\n⏰ 撤回: {format_duration_cn(item['recall_after_seconds'])}"

            yield f"✅ 提醒 '{name}' 添加成功！\n📅 Cron: {cron_expr}\n📍 目标: {describe_origin(target_origin)}{recall_info}"

        except Exception as e:
            logger.error(f"添加提醒失败: {e}", exc_info=True)
            yield f"添加提醒失败: {e}"

    async def edit_reminder(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """编辑定时提醒"""
        try:
            from .utils import resolve_session_origin, summarize_message_structure
            from astrbot.core.platform.astrbot_message import MessageType

            parts = event.message_str.strip().split(maxsplit=2)
            if len(parts) < 2:
                yield "❌ 参数缺失！用法: /编辑提醒 <提醒名称或序号> [@好友号|#群号] [cron表达式] [消息内容]\n提示: 所有参数都可选，可单独或组合编辑"
                return

            identifier, remaining = parts[1], parts[2] if len(parts) > 2 else ""

            # 查找目标提醒（支持名称或序号）
            target_item = None
            if identifier.isdigit():
                # 使用序号查找
                idx = int(identifier) - 1
                reminders_only = [item for item in self.plugin.reminders if not item.get('is_task', False)]
                if 0 <= idx < len(reminders_only):
                    target_item = reminders_only[idx]
                else:
                    yield f"❌ 序号无效，请输入1-{len(reminders_only)}之间的数字"
                    return
            else:
                # 使用名称查找
                name = identifier
                for item in self.plugin.reminders:
                    if not item.get('is_task', False) and item.get('name') == name:
                        target_item = item
                        break

            if not target_item:
                yield f"❌ 未找到名为 '{identifier}' 的提醒"
                return

            # 如果没有提供剩余参数，显示当前配置
            if not remaining.strip():
                current_cron = target_item.get('cron_expr', target_item.get('cron', '未设置'))
                message_structure = target_item.get('message_structure', [])
                current_content = "未设置"
                if message_structure:
                    summary = summarize_message_structure(message_structure)
                    parts = []
                    if summary.get('text', 0) > 0:
                        parts.append(f"{summary['text']}段文字")
                    if summary.get('image', 0) > 0:
                        parts.append(f"{summary['image']}张图片")
                    if parts:
                        current_content = "、".join(parts)

                enabled_sessions = target_item.get('enabled_sessions', [])
                sessions_info = ""
                if enabled_sessions:
                    sessions_info = "\n📍 当前会话:\n" + "\n".join([f"  - {describe_origin(s)}" for s in enabled_sessions])
                else:
                    sessions_info = "\n📍 当前会话: 无"

                yield f"📋 当前提醒配置：\nCron: {current_cron}\n内容: {current_content}{sessions_info}"
                return

            # 检查是否包含会话参数
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

            # 重新组合剩余参数用于解析cron和内容
            remaining = ' '.join(filtered_remaining)

            # 解析参数
            cron_expr, content_text = self._parse_edit_params(remaining)

            # 如果都没有提供，显示帮助
            if not cron_expr and not content_text and not session_params:
                yield "❌ 请提供要修改的cron表达式、内容或会话！\n用法: /编辑提醒 <提醒名称或序号> [@好友号|#群号] [cron表达式] [内容]"
                return

            # 更新cron表达式
            if cron_expr:
                target_item['cron_expr'] = cron_expr

            # 更新内容
            if content_text:
                await self._process_reminder_content(event, target_item, content_text, cron_expr or target_item.get('cron_expr', ''))

            # 保存更新
            self.plugin._save_reminders()

            # 如果有任何变更，重新调度
            if session_changed or cron_expr:
                await self.plugin._reschedule_reminder(target_item)

            updates = []
            if session_changed:
                new_sessions = target_item.get('enabled_sessions', [])
                sessions_str = ", ".join([describe_origin(s) for s in new_sessions])
                updates.append(f"✅ 会话已更新: {sessions_str}")
            if cron_expr:
                updates.append(f"✅ Cron表达式已更新: {cron_expr}")
            if content_text:
                updates.append(f"✅ 内容已更新")

            yield f"🎉 提醒 '{target_item['name']}' 编辑成功！\n" + "\n".join(updates)

        except Exception as e:
            logger.error(f"编辑提醒时出错: {e}", exc_info=True)
            yield f"❌ 编辑提醒时出错: {e}"

    async def delete_reminder(self, event: AstrMessageEvent, key: str = None) -> AsyncGenerator[str, None]:
        """删除定时提醒"""
        try:
            if not key:
                yield "❌ 参数缺失！用法: /删除提醒 <提醒名称或序号>"
                return

            # 查找目标提醒
            target_index = None
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(self.plugin.reminders):
                    item = self.plugin.reminders[idx]
                    if not item.get('is_task', False):
                        target_index = idx
            else:
                for i, item in enumerate(self.plugin.reminders):
                    if not item.get('is_task', False) and item.get('name') == key:
                        target_index = i
                        break

            if target_index is None:
                yield f"❌ 未找到提醒: {key}"
                return

            # 移除调度
            target_item = self.plugin.reminders[target_index]
            job_id = build_job_id(target_item['name'], False)
            if job_id in self.plugin.scheduler.get_jobs():
                self.plugin.scheduler.remove_job(job_id)

            # 删除关联的媒体文件
            for msg_item in target_item.get('message_structure', []):
                if msg_item.get('type') in ('image', 'record', 'video'):
                    img_path = os.path.join(self.plugin.data_dir, msg_item['path'])
                    try:
                        if os.path.exists(img_path):
                            os.remove(img_path)
                    except Exception as e:
                        logger.error(f"删除媒体文件失败: {e}")

            # 删除关联的链接任务
            reminder_name = target_item['name']
            if reminder_name in self.plugin.linked_tasks:
                del self.plugin.linked_tasks[reminder_name]
                logger.info(f"已删除提醒 '{reminder_name}' 的链接任务")

            # 从列表中移除
            self.plugin.reminders.pop(target_index)

            # 保存数据
            self.plugin._save_reminders()

            yield f"✅ 提醒 '{target_item['name']}' 已删除"

        except Exception as e:
            logger.error(f"删除提醒时出错: {e}", exc_info=True)
            yield f"❌ 删除提醒时出错: {e}"

    async def list_reminders(self, event: AstrMessageEvent, name: str = "") -> AsyncGenerator[str, None]:
        """查看提醒"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            if not self.plugin.reminders:
                yield "当前没有任务/提醒"
                return

            # 筛选提醒
            items = [item for item in self.plugin.reminders if not item.get('is_task', False)]

            if not items:
                yield "当前没有提醒"
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
                    yield f"❌ 未找到名为 '{params}' 的提醒\n\n💡 使用 /查看提醒 查看所有提醒列表"
                    return

                # 构建详情消息
                from .utils import split_message_structure
                from astrbot.api.message_components import Plain

                enabled_sessions = target_item.get('enabled_sessions', [])
                info_text = f"📋 提醒详情: {target_item['name']}\n\n"
                if enabled_sessions:
                    info_text += "🎯 已启用会话:\n"
                    for s in enabled_sessions:
                        info_text += f"{describe_origin(s)}\n"
                else:
                    info_text += "🎯 当前未在任何会话启用\n"

                info_text += f"⏰ 定时规则: {target_item.get('cron', target_item.get('cron_expr', 'N/A'))}\n"
                info_text += f"📅 创建时间: {target_item.get('created_at', 'N/A')}\n"
                info_text += f"👤 创建者ID: {target_item.get('created_by', '未知')}\n"
                info_text += f"\n📝 提醒内容:\n"
                recall_after = int(target_item.get('recall_after_seconds', 0) or 0)
                if recall_after > 0:
                    info_text += f"⏪ 撤回时间: {format_duration_cn(recall_after)}\n"

                # 构建完整的消息链
                message_components = [Plain(info_text)]

                # 添加内容预览到同一消息
                chunks = split_message_structure(target_item['message_structure'])
                for chunk in chunks:
                    preview_chain = build_message_chain_from_structure(chunk, self.plugin.data_dir, logger)
                    if preview_chain:
                        message_components.extend(preview_chain)

                # 添加链接任务信息
                reminder_name = target_item['name']
                if reminder_name in self.plugin.linked_tasks and self.plugin.linked_tasks[reminder_name]:
                    linked_commands = self.plugin.linked_tasks[reminder_name]
                    linked_info = f"\n\n🔆 {target_item['name']} 已链接的任务:\n"
                    for i, cmd_data in enumerate(linked_commands, 1):
                        cmd_str = cmd_data if isinstance(cmd_data, str) else cmd_data.get('command', '')
                        linked_info += f"  {i}. {cmd_str}\n"
                    message_components.append(Plain(linked_info))

                # 一次性返回完整消息链
                yield {"type": "message_chain", "data": message_components}
            else:
                # 显示所有提醒列表（简略信息）
                result = f"📋 当前提醒列表:\n\n"
                for idx, item in enumerate(items, 1):
                    result += f"{idx}. {item['name']}\n"

                yield result

        except Exception as e:
            logger.error(f"查看提醒时出错: {e}", exc_info=True)
            yield f"❌ 查看提醒时出错: {e}"

    async def toggle_reminder_session(self, event: AstrMessageEvent, enable: bool) -> AsyncGenerator[str, None]:
        """启动或停止提醒"""
        try:
            from .utils import resolve_session_origin
            from astrbot.core.platform.astrbot_message import MessageType

            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            parts = event.message_str.strip().split()
            action = "启动" if enable else "停止"

            if len(parts) < 2:
                yield f"❌ 参数缺失！\n用法: /{action}提醒 <提醒名> [@好友号|#群号 ...]"
                return

            name = parts[1]
            session_params = parts[2:]

            if session_params:
                invalid_params = [p for p in session_params if not p.startswith('@') and not p.startswith('#')]
                if invalid_params:
                    invalid_str = " ".join(invalid_params)
                    yield f"❌ 会话参数无效: {invalid_str}\n用法: /{action}提醒 <提醒名> [@好友号|#群号 ...]"
                    return
                raw_targets = session_params
            else:
                raw_targets = [None]

            target_item = None
            for item in self.plugin.reminders:
                if not item.get('is_task', False) and item.get('name') == name:
                    target_item = item
                    break

            if not target_item:
                yield f"❌ 未找到名为 '{name}' 的提醒"
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
            unsupported_recall_sessions: List[str] = []

            for session in resolved_sessions:
                if enable:
                    if session in enabled_sessions:
                        skipped_sessions.append(session)
                        continue
                    enabled_sessions.append(session)
                    self.plugin._add_job(target_item, session)
                    success_sessions.append(session)
                    changed = True

                    if int(target_item.get('recall_after_seconds', 0) or 0) > 0:
                        recall_ok, recall_reason = self.plugin._check_recall_capability(session)
                        if not recall_ok:
                            unsupported_recall_sessions.append(f"{describe_origin(session)}（{recall_reason}）")
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

            lines = [f"✅ {action}提醒完成: {name}"]

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

            if unsupported_recall_sessions:
                lines.append("⚠️ 以下会话不支持自动撤回，将仅发送不撤回: " + "、".join(unsupported_recall_sessions))

            yield "\n".join(lines)

        except Exception as e:
            logger.error(f"{action}提醒时出错: {e}", exc_info=True)
            yield f"❌ {action}提醒时出错: {e}"

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

    async def send_reminder(self, item: Dict, session: str):
        """发送定时提醒"""
        try:
            message_structure = item.get('message_structure', [])
            if not message_structure:
                logger.warning(f"提醒 '{item.get('name')}' 没有内容，跳过发送")
                return

            # 按平台限制拆分消息
            message_chunks = split_message_structure(message_structure)

            if not message_chunks:
                logger.warning(f"提醒 '{item.get('name')}' 消息为空")
                return

            recall_after_seconds = int(item.get('recall_after_seconds', 0) or 0)
            message_ids: List[int | str] = []

            # 检查撤回支持
            recall_supported = False
            recall_reason = ""
            if recall_after_seconds > 0:
                recall_supported, recall_reason = self.plugin._check_recall_capability(session)
                if not recall_supported:
                    logger.warning(
                        f"提醒 '{item.get('name', 'unknown')}' 配置了自动撤回，但{recall_reason}，本次仅发送不撤回")
                    await self.plugin._notify_recall_not_supported_once(item, session, recall_reason)

            for chunk in message_chunks:
                chain = build_message_chain_from_structure(
                    chunk,
                    self.plugin.data_dir,
                    logger
                )

                if not chain:
                    continue

                message_id = None
                if recall_after_seconds > 0 and recall_supported:
                    message_id = await self.plugin._send_aiocqhttp_with_message_id(
                        {'message_structure': chunk},
                        session,
                    )

                if message_id is None:
                    message_chain = MessageChain()
                    message_chain.chain = chain
                    send_ret = await self.plugin.context.send_message(session, message_chain)
                    message_id = extract_message_id(send_ret)

                if message_id is not None:
                    message_ids.append(message_id)

            if recall_after_seconds > 0 and recall_supported:
                if message_ids:
                    for message_id in message_ids:
                        asyncio.create_task(
                            self.plugin._recall_message_later(session, message_id, recall_after_seconds))
                else:
                    logger.warning(f"提醒已发送但未拿到 message_id，无法自动撤回: {item.get('name')}")

            logger.info(f"提醒 '{item.get('name')}' 已发送到 {session}")

            # 处理链接任务
            linked_commands = self.plugin.linked_tasks.get(item.get('name'), [])
            if linked_commands:
                tasks = []
                for linked_command in linked_commands:
                    task = asyncio.create_task(self._execute_linked_command(linked_command, session, item))
                    tasks.append(task)

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"发送提醒 '{item.get('name')}' 时出错: {e}", exc_info=True)

    async def _handle_recall(self, item: Dict, session: str, delay_seconds: int):
        """处理消息撤回"""
        try:
            # 检查平台是否支持撤回
            recall_ok, reason = self.plugin._check_recall_capability(session)
            if not recall_ok:
                if session not in self.plugin._recall_notice_sent:
                    self.plugin._recall_notice_sent.add(session)
                    logger.info(f"会话 {session} 不支持自动撤回: {reason}")
                return

            # 发送消息并获取消息ID
            message_id = await self.plugin._send_aiocqhttp_with_message_id(item, session)
            if message_id:
                # 延迟撤回
                await self.plugin._recall_message_later(session, message_id, delay_seconds)

        except Exception as e:
            logger.error(f"处理撤回时出错: {e}", exc_info=True)

    def _resolve_target_session(self, event: AstrMessageEvent, target_session: str) -> Optional[str]:
        """解析目标会话"""
        # 这里需要根据实际需求实现
        # 暂时返回当前会话
        return event.unified_msg_origin

    def _parse_cron_and_content(self, remaining: str) -> tuple:
        """解析cron表达式和内容"""
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

        # 提取内容
        content_text = ""
        if len(remaining_parts) > 5:
            content_text = remaining_parts[5]
        if len(last_part) > len(cleaned_last_part):
            content_text = last_part[len(cleaned_last_part):] + (' ' + content_text if content_text else '')

        return cron_expr, content_text.strip()

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

        # 获取内容部分
        content_text = None
        if len(remaining_parts) > 5:
            content_text = remaining_parts[5].strip()
        elif cron_expr is None:
            content_text = remaining.strip()
        else:
            last_part = remaining_parts[4]
            cleaned_last_part = self._clean_cron_part(last_part)
            if len(last_part) > len(cleaned_last_part):
                content_text = last_part[len(cleaned_last_part):].strip()
            if len(remaining_parts) > 5 and remaining_parts[5]:
                content_text = (content_text + " " + remaining_parts[5]).strip()

        return cron_expr, content_text

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

    async def _process_reminder_content(self, event, item: Dict, content_text: str, cron_expr: str):
        """处理提醒内容"""
        recall_after_seconds = None
        recall_time_token = ""

        # 解析撤回时间
        recall_after_seconds, cleaned_content = parse_recall_seconds(content_text)
        if recall_after_seconds is not None:
            recall_time_token = content_text.lstrip().split(maxsplit=1)[0]
            content_text = cleaned_content

        # 提取消息结构
        inline_structure = await extract_inline_message_structure(
            event.get_messages(),
            cron_expr,
            lambda comp: self.plugin._save_media_component(comp, getattr(comp, 'type', 'media').lower()),
        )
        inline_structure = normalize_message_structure(inline_structure, recall_time_token)

        reply_structure = await fetch_reply_message_structure(
            event,
            self.plugin._get_platform_adapter_name,
            self.plugin._get_platform_api_client,
            self.plugin._save_media_component,
            logger,
        )

        # 合并消息结构
        message_structure = []
        if inline_structure and reply_structure:
            message_structure = inline_structure + reply_structure
        elif inline_structure:
            message_structure = inline_structure
        elif reply_structure:
            message_structure = reply_structure

        if not message_structure:
            raise ValueError("提醒内容不能为空，请至少提供文字或图片")

        item['message_structure'] = message_structure
        item['recall_after_seconds'] = recall_after_seconds or 0

    async def _execute_linked_command(self, linked_task_data: str | Dict, unified_msg_origin: str, item: Dict):
        """执行单个链接任务"""
        from astrbot.api.message_components import At, Face, Plain
        from .utils import normalize_message_structure, collect_text_from_message_structure

        is_admin = item.get('is_admin', True)
        self_id = item.get('self_id')

        command = ""
        original_components = []

        if isinstance(linked_task_data, str):
            command = linked_task_data
        elif isinstance(linked_task_data, dict):
            command = linked_task_data.get('command', '')
            # 还原组件，并兼容历史占位写法（[at:xxx]/[atall]）
            message_structure = linked_task_data.get('message_structure', [])
            if isinstance(message_structure, list):
                message_structure = normalize_message_structure(message_structure)
                for comp in message_structure:
                    if comp['type'] == 'at':
                        original_components.append(At(qq=comp['qq']))
                    elif comp['type'] == 'atall':
                        original_components.append(At(qq="all"))
                    elif comp['type'] == 'face':
                        original_components.append(Face(id=comp['id']))

        # 兼容历史链接任务：也从 command 文本中解析 [at:xxx]/[atall]
        cmd_structure = normalize_message_structure([{'type': 'text', 'content': command}])
        parsed_command = collect_text_from_message_structure(cmd_structure).strip()
        if parsed_command:
            command = parsed_command
        for comp in cmd_structure:
            if comp.get('type') == 'at':
                original_components.append(At(qq=comp.get('qq', '')))
            elif comp.get('type') == 'atall':
                original_components.append(At(qq="all"))

        if command:
            await self.plugin._execute_command_common(command, unified_msg_origin, item, "链接任务",
                                                       original_components=original_components, is_admin=is_admin,
                                                       self_id=self_id)

    async def send_now(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """立即发送提醒"""
        try:
            import re

            # 解析参数
            parts = event.message_str.strip().split(maxsplit=1)
            if len(parts) < 2:
                yield "❌ 参数缺失！用法: /发送提醒 <提醒名称或序号>"
                return

            key = parts[1].strip()

            # 查找提醒
            item = None
            if re.fullmatch(r"\d+", key):
                # 按序号查找
                index = int(key) - 1
                reminder_list = [i for i in self.plugin.reminders if not i.get('is_task', False)]
                if 0 <= index < len(reminder_list):
                    item = reminder_list[index]
            else:
                # 按名称查找
                for i in self.plugin.reminders:
                    if i.get('name') == key and not i.get('is_task', False):
                        item = i
                        break

            if not item:
                yield f"❌ 未找到提醒: {key}"
                return

            # 在所有启用的会话中发送
            enabled_sessions = item.get('enabled_sessions', [])
            if not enabled_sessions:
                yield f"❌ 提醒 '{item.get('name')}' 未在任何会话启用"
                return

            for session in enabled_sessions:
                await self.send_reminder(item, session)

            yield f"✅ 提醒 '{item.get('name')}' 已发送到 {len(enabled_sessions)} 个会话"

        except Exception as e:
            logger.error(f"发送提醒失败: {e}", exc_info=True)
            yield f"❌ 发送提醒失败: {e}"