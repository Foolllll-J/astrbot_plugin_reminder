"""
提醒管理器 - 处理定时提醒相关的所有业务逻辑
"""

import os
import asyncio
import re
import datetime
from typing import Dict, List, AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Face, Plain
from astrbot.core.platform.astrbot_message import MessageType
from apscheduler.triggers.cron import CronTrigger

from .utils import (
    describe_origin,
    execute_linked_command,
    extract_inline_message_structure,
    extract_message_structure_after_token_count,
    fetch_reply_message_structure,
    format_duration_cn,
    normalize_message_structure,
    normalize_session_param_token,
    parse_recall_seconds,
    resolve_session_origin,
    split_message_structure,
    summarize_message_structure,
    translate_to_apscheduler_cron,
    build_message_chain_from_structure,
    extract_message_id,
    is_user_allowed,
)


class ReminderManager:
    """提醒管理器类"""

    def __init__(self, plugin):
        self.plugin = plugin

    async def add_reminder(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """添加定时提醒"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

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
                'created_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            # 处理提醒内容
            used_inline_content = False
            used_reply_content = False

            recall_after_seconds = None
            recall_time_token = ""
            recall_after_seconds, cleaned_content = parse_recall_seconds(content_text)
            if recall_after_seconds is not None:
                recall_time_token = content_text.lstrip().split(maxsplit=1)[0]
                content_text = cleaned_content

            inline_structure = await extract_inline_message_structure(
                event.get_messages(),
                cron_expr,
                lambda comp: self.plugin._save_media_component(comp, getattr(comp, 'type', 'media').lower()),
            )
            inline_structure = normalize_message_structure(inline_structure, recall_time_token)
            used_inline_content = bool(inline_structure)

            reply_structure = await fetch_reply_message_structure(
                event,
                self.plugin._get_platform_adapter_name,
                self.plugin._get_platform_api_client,
                self.plugin._save_media_component,
                logger,
            )

            message_structure = []
            if inline_structure and reply_structure:
                message_structure = inline_structure + reply_structure
                used_reply_content = True
            elif inline_structure:
                message_structure = inline_structure
            elif reply_structure:
                message_structure = reply_structure
                used_reply_content = True

            if not message_structure and content_text.strip():
                text_structure = [{'type': 'text', 'content': content_text.strip()}]
                text_structure = normalize_message_structure(text_structure, recall_time_token)
                message_structure = text_structure
                used_inline_content = True

            if not message_structure:
                yield "提醒内容不能为空，请至少提供文字或图片"
                return

            item['message_structure'] = message_structure
            if recall_after_seconds is not None:
                item['recall_after_seconds'] = recall_after_seconds

            # 添加到列表
            self.plugin.reminders.append(item)

            # 调度提醒
            await self.plugin._schedule_reminder(item)

            # 保存数据
            self.plugin._save_reminders()

            summary = summarize_message_structure(item.get('message_structure', []))
            text_count = summary['text']
            image_count = summary['image']
            face_count = summary['face']
            at_count = summary['at'] + summary['atall']
            record_count = summary['record']
            video_count = summary['video']

            result_msg = f"✅ 提醒 '{name}' 添加成功！\nCron: {cron_expr}\n目标: {describe_origin(target_origin)}"
            if text_count > 0:
                result_msg += f"\n文字: {text_count}段"
            if image_count > 0:
                result_msg += f"\n图片: {image_count}张"
            if face_count > 0:
                result_msg += f"\n表情: {face_count}个"
            if at_count > 0:
                result_msg += f"\nAt: {at_count}人"
            if record_count > 0:
                result_msg += f"\n语音: {record_count}条"
            if video_count > 0:
                result_msg += f"\n视频: {video_count}条"
            if item.get('recall_after_seconds', 0) > 0:
                result_msg += f"\n撤回: {format_duration_cn(item['recall_after_seconds'])}"
            if used_inline_content and used_reply_content:
                result_msg += "\n内容来源: 指令正文 + 引用消息"
            elif used_reply_content:
                result_msg += "\n内容来源: 引用消息"
            elif used_inline_content:
                result_msg += "\n内容来源: 指令正文"

            if item.get('recall_after_seconds', 0) > 0:
                recall_ok, recall_reason = self.plugin._check_recall_capability(target_origin)
                if not recall_ok:
                    result_msg += f"\n⚠️ 已设置撤回，但{recall_reason}，将仅发送不撤回。"

            yield result_msg

        except Exception as e:
            logger.error(f"添加提醒失败: {e}", exc_info=True)
            yield f"添加提醒失败: {e}"

    async def edit_reminder(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """编辑定时提醒"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            parts = event.message_str.strip().split(maxsplit=2)
            if len(parts) < 2:
                yield "❌ 参数缺失！用法: /编辑提醒 <提醒名称或序号> [@好友号|#群号] [cron表达式] [消息内容]\n提示: 所有参数都可选，可单独或组合编辑"
                return

            identifier, remaining = parts[1], parts[2] if len(parts) > 2 else ""
            tokens_all = self._extract_message_tokens(event)
            session_params, remaining_tokens = self._split_edit_remaining_tokens(tokens_all)

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
            if not remaining_tokens and not session_params:
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

            # 解析参数
            cron_expr, content_text = self._parse_edit_params_from_tokens(remaining_tokens)
            if content_text is not None and not content_text.strip():
                content_text = None

            # 如果都没有提供，显示帮助
            if not cron_expr and not content_text and not session_params:
                yield "❌ 请提供要修改的cron表达式、内容或会话！\n用法: /编辑提醒 <提醒名称或序号> [@好友号|#群号] [cron表达式] [内容]"
                return

            # 更新cron表达式
            if cron_expr:
                target_item['cron_expr'] = cron_expr

            # 更新内容（仅在 content_text 非空时处理）
            recall_changed = False
            content_changed = False
            used_inline_content = False
            used_reply_content = False
            if content_text is not None and content_text.strip():
                recall_only_seconds, cleaned_content = parse_recall_seconds(content_text)
                if recall_only_seconds is not None and not cleaned_content.strip():
                    target_item['recall_after_seconds'] = recall_only_seconds
                    recall_changed = True
                else:
                    used_inline_content, used_reply_content = await self._process_reminder_content(
                        event,
                        target_item,
                        content_text,
                        cron_expr or target_item.get('cron_expr', ''),
                    )
                    content_changed = True

            # 如果有任何变更，记录编辑时间
            if session_changed or cron_expr or content_changed or recall_changed:
                target_item['edited_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                target_item['created_by'] = event.get_sender_id()
                target_item['creator_name'] = event.get_sender_name()
                target_item['is_admin'] = event.is_admin()

            # 保存更新
            self.plugin._save_reminders()

            # 如果有任何变更，重新调度
            if session_changed or cron_expr:
                await self.plugin._reschedule_reminder(target_item)

            current_cron = target_item.get('cron_expr', target_item.get('cron', ''))
            summary = summarize_message_structure(target_item.get('message_structure', []))
            text_count = summary['text']
            image_count = summary['image']
            face_count = summary['face']
            at_count = summary['at'] + summary['atall']
            record_count = summary['record']
            video_count = summary['video']
            sessions = list(target_item.get('enabled_sessions', []))
            session_count = len(sessions)

            msg = (
                f"✅ 提醒已编辑！\n"
                f"名称: {target_item.get('name', '')}\n"
                f"cron: {current_cron}"
            )
            if text_count > 0:
                msg += f"\n文字: {text_count}段"
            if image_count > 0:
                msg += f"\n图片: {image_count}张"
            if face_count > 0:
                msg += f"\n表情: {face_count}个"
            if at_count > 0:
                msg += f"\nAt: {at_count}人"
            if record_count > 0:
                msg += f"\n语音: {record_count}条"
            if video_count > 0:
                msg += f"\n视频: {video_count}条"
            if target_item.get('recall_after_seconds', 0) > 0:
                msg += f"\n撤回: {format_duration_cn(target_item['recall_after_seconds'])}"
            if used_inline_content and used_reply_content:
                msg += "\n内容来源: 指令正文 + 引用消息"
            elif used_reply_content:
                msg += "\n内容来源: 引用消息"
            elif used_inline_content:
                msg += "\n内容来源: 指令正文"
            msg += f"\n已影响会话数: {session_count}"

            if target_item.get('recall_after_seconds', 0) > 0 and sessions:
                unsupported_sessions = []
                for s in sessions:
                    recall_ok, _ = self.plugin._check_recall_capability(s)
                    if not recall_ok:
                        unsupported_sessions.append(describe_origin(s))
                if unsupported_sessions:
                    display_sessions = "、".join(unsupported_sessions[:3])
                    if len(unsupported_sessions) > 3:
                        display_sessions += f" 等{len(unsupported_sessions)}个会话"
                    msg += f"\n⚠️ 以下会话不支持自动撤回: {display_sessions}（将仅发送不撤回）"

            yield msg

        except Exception as e:
            logger.error(f"编辑提醒时出错: {e}", exc_info=True)
            yield f"❌ 编辑提醒时出错: {e}"

    async def delete_reminder(self, event: AstrMessageEvent, key: str = None) -> AsyncGenerator[str, None]:
        """删除定时提醒"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

            if not key:
                yield "❌ 参数缺失！用法: /删除提醒 <提醒名称或序号>"
                return

            # 获取过滤后的提醒列表（只包含提醒，不包含任务）
            reminder_items = [item for item in self.plugin.reminders if not item.get('is_task', False)]

            # 查找目标提醒
            target_index = None
            if key.isdigit():
                idx = int(key) - 1
                if 0 <= idx < len(reminder_items):
                    target_item = reminder_items[idx]
                    # 在原始列表中找到对应的索引
                    for i, item in enumerate(self.plugin.reminders):
                        if item['name'] == target_item['name']:
                            target_index = i
                            break
            else:
                for i, item in enumerate(reminder_items):
                    if item.get('name') == key:
                        # 在原始列表中找到对应的索引
                        for j, orig_item in enumerate(self.plugin.reminders):
                            if orig_item['name'] == key:
                                target_index = j
                                break
                        break

            if target_index is None:
                yield f"❌ 未找到提醒: {key}"
                return

            # 移除调度
            target_item = self.plugin.reminders[target_index]
            self.plugin._remove_all_jobs_for_item(target_item)

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
                enabled_sessions = target_item.get('enabled_sessions', [])
                info_text = f"📋 提醒详情: {target_item['name']}\n\n"
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

                # 撤回时间作为配置参数，放在基本信息中
                recall_after = int(target_item.get('recall_after_seconds', 0) or 0)
                if recall_after > 0:
                    info_text += f"⏪ 撤回时间: {format_duration_cn(recall_after)}\n"

                info_text += f"\n📝 提醒内容:\n"

                # 构建完整的消息链
                chunks = split_message_structure(target_item['message_structure'])
                has_standalone_media = any(
                    any(item.get("type") in ("record", "video") for item in chunk)
                    for chunk in chunks
                )

                if has_standalone_media:
                    # 语音/视频需要单独发送，其它内容合并到一条消息中
                    message_components = [Plain(info_text)]
                    standalone_chunks = []
                    for chunk in chunks:
                        if any(item.get("type") in ("record", "video") for item in chunk):
                            standalone_chunks.append(chunk)
                            continue
                        preview_chain = build_message_chain_from_structure(chunk, self.plugin.data_dir, logger)
                        if preview_chain:
                            message_components.extend(preview_chain)
                else:
                    message_components = [Plain(info_text)]
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

                # 先发送合并后的文本/图片/表情内容
                yield {"type": "message_chain", "data": message_components}

                # 再单独发送语音/视频
                if has_standalone_media:
                    for chunk in standalone_chunks:
                        preview_chain = build_message_chain_from_structure(chunk, self.plugin.data_dir, logger)
                        if preview_chain:
                            yield {"type": "message_chain", "data": preview_chain}
            else:
                # 显示所有提醒列表（简略信息）
                result = "📋 当前提醒列表:\n\n"
                for idx, item in enumerate(items, 1):
                    result += f"{idx}. {item['name']}\n"

                    enabled_sessions = item.get('enabled_sessions', [])
                    if enabled_sessions:
                        result += f"   已启用会话数: {len(enabled_sessions)}\n"
                    else:
                        result += "   已启用会话数: 0\n"

                    result += f"   cron: {item.get('cron_expr', item.get('cron', 'N/A'))}\n"

                    summary = summarize_message_structure(item.get('message_structure', []))
                    text_count = summary['text']
                    image_count = summary['image']
                    face_count = summary['face']
                    at_count = summary['at']
                    atall_count = summary['atall']
                    record_count = summary['record']
                    video_count = summary['video']

                    content_parts = []
                    if text_count > 0:
                        content_parts.append(f"文字{text_count}段")
                    if image_count > 0:
                        content_parts.append(f"图片{image_count}张")
                    if face_count > 0:
                        content_parts.append(f"表情{face_count}个")
                    if at_count > 0:
                        content_parts.append(f"At{at_count}人")
                    if atall_count > 0:
                        content_parts.append(f"@全体{atall_count}次")
                    if record_count > 0:
                        content_parts.append(f"语音{record_count}条")
                    if video_count > 0:
                        content_parts.append(f"视频{video_count}条")

                    if content_parts:
                        result += f"   内容: {' + '.join(content_parts)}\n"

                    recall_after = int(item.get('recall_after_seconds', 0) or 0)
                    if recall_after > 0:
                        result += f"   撤回: {format_duration_cn(recall_after)}\n"

                    reminder_name = item['name']
                    if reminder_name in self.plugin.linked_tasks and self.plugin.linked_tasks[reminder_name]:
                        linked_count = len(self.plugin.linked_tasks[reminder_name])
                        result += f"   🔗 链接任务: {linked_count}个\n"

                    result += f"   创建时间: {item.get('created_at', 'N/A')}\n\n"

                result += "💡 使用 /查看提醒 <序号或名称> 查看详细内容"
                yield result

        except Exception as e:
            logger.error(f"查看提醒时出错: {e}", exc_info=True)
            yield f"❌ 查看提醒时出错: {e}"

    async def toggle_reminder_session(self, event: AstrMessageEvent, enable: bool) -> AsyncGenerator[str, None]:
        """启动或停止提醒"""
        try:
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
                invalid_params = [
                    p for p in session_params if normalize_session_param_token(p) is None
                ]
                if invalid_params:
                    invalid_str = " ".join(invalid_params)
                    yield f"❌ 会话参数无效: {invalid_str}\n用法: /{action}提醒 <提醒名> [@好友号|#群号 ...]"
                    return
                raw_targets = [normalize_session_param_token(p) for p in session_params if normalize_session_param_token(p)]
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
        return is_user_allowed(self.plugin, event)

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

    def _parse_edit_params_from_tokens(self, tokens: List[str]) -> tuple:
        """从 token 列表解析 cron 与内容。"""
        cron_expr = None
        content_start_idx = 0

        if len(tokens) >= 5:
            cron_candidate = tokens[:5]
            try:
                cleaned_last = self._clean_cron_part(cron_candidate[4])
                if cleaned_last:
                    cron_candidate[4] = cleaned_last
                    test_cron = ' '.join(cron_candidate)
                    CronTrigger.from_crontab(translate_to_apscheduler_cron(test_cron))
                    cron_expr = test_cron
                    content_start_idx = 5
            except Exception:
                pass

        if content_start_idx < len(tokens):
            content_text = ' '.join(tokens[content_start_idx:])
        else:
            content_text = None

        return cron_expr, content_text

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

    async def _process_reminder_content(self, event, item: Dict, content_text: str, cron_expr: str):
        """处理提醒内容（新增和编辑都使用相同逻辑）"""
        used_inline_content = False
        used_reply_content = False
        recall_after_seconds = None
        recall_time_token = ""

        # 解析撤回时间
        recall_after_seconds, cleaned_content = parse_recall_seconds(content_text)
        if recall_after_seconds is not None:
            recall_time_token = content_text.lstrip().split(maxsplit=1)[0]
            content_text = cleaned_content

        # 从事件消息中提取内容（与新增时保持一致）
        inline_structure = await extract_inline_message_structure(
            event.get_messages(),
            cron_expr,
            lambda comp: self.plugin._save_media_component(comp, getattr(comp, 'type', 'media').lower()),
        )
        inline_structure = normalize_message_structure(inline_structure, recall_time_token)
        used_inline_content = bool(inline_structure)

        if not inline_structure and content_text.strip():
            token_offset = self._calc_edit_content_token_offset(event)
            ordered_structure = await extract_message_structure_after_token_count(
                event.get_messages(),
                token_offset,
                lambda comp: self.plugin._save_media_component(comp, getattr(comp, 'type', 'media').lower()),
            )
            ordered_structure = normalize_message_structure(ordered_structure, recall_time_token)
            if ordered_structure:
                inline_structure = ordered_structure
                used_inline_content = True

        reply_structure = await fetch_reply_message_structure(
            event,
            self.plugin._get_platform_adapter_name,
            self.plugin._get_platform_api_client,
            self.plugin._save_media_component,
            logger,
        )
        used_reply_content = bool(reply_structure)

        # 合并消息结构
        message_structure = []
        if inline_structure and reply_structure:
            message_structure = inline_structure + reply_structure
        elif inline_structure:
            message_structure = inline_structure
        elif reply_structure:
            message_structure = reply_structure

        # 如果没有从消息链中提取到内容，但提供了 content_text，则作为文本内容
        if not message_structure and content_text.strip():
            text_structure = [{'type': 'text', 'content': content_text.strip()}]
            text_structure = normalize_message_structure(text_structure, recall_time_token)
            message_structure = text_structure

        if not message_structure:
            raise ValueError("提醒内容不能为空，请至少提供文字或图片")

        item['message_structure'] = message_structure
        if recall_after_seconds is not None:
            item['recall_after_seconds'] = recall_after_seconds

        return used_inline_content, used_reply_content

    def _calc_edit_content_token_offset(self, event: AstrMessageEvent) -> int:
        """计算编辑提醒时内容在文本 token 序列中的起始偏移。"""
        tokens = self._extract_message_tokens(event)
        if len(tokens) < 2:
            return 0

        session_params, remaining_tokens = self._split_edit_remaining_tokens(tokens)
        cron_expr, _ = self._parse_edit_params_from_tokens(remaining_tokens)
        if cron_expr:
            return 2 + len(session_params) + 5
        return 2 + len(session_params)

    async def _execute_linked_command(self, linked_task_data: str | Dict, unified_msg_origin: str, item: Dict):
        """执行单个链接任务"""
        logger.info(f"开始执行链接任务: {linked_task_data}")

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
        # 注意：不要使用 collect_text_from_message_structure，因为它会丢弃非文本内容（如唤醒前缀）
        cmd_structure = normalize_message_structure([{'type': 'text', 'content': command}])
        for comp in cmd_structure:
            if comp.get('type') == 'at':
                original_components.append(At(qq=comp.get('qq', '')))
            elif comp.get('type') == 'atall':
                original_components.append(At(qq="all"))

        # 去重组件，避免重复 At/AtAll/Face
        deduped = []
        seen = set()
        for comp in original_components:
            if isinstance(comp, At):
                key = ("at", "all" if str(comp.qq).lower() == "all" else str(comp.qq))
            elif isinstance(comp, Face):
                key = ("face", str(comp.id))
            else:
                key = (comp.__class__.__name__, str(comp))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(comp)
        original_components = deduped

        logger.info(f"执行链接任务命令: {command}, 组件数: {len(original_components)}")

        if command:
            await execute_linked_command(self.plugin, command, unified_msg_origin, item,
                                        original_components=original_components, is_admin=is_admin,
                                        self_id=self_id)

    async def send_now(self, event: AstrMessageEvent) -> AsyncGenerator[str, None]:
        """立即发送提醒"""
        try:
            # 权限检查
            if not self._is_allowed(event):
                yield "❌ 抱歉，你没有权限使用该指令。"
                return

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
