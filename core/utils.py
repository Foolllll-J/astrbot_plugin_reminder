import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Dict, List, Optional

from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Face, Image, Plain, Record, Video


def translate_to_apscheduler_cron(cron_expr: str) -> str:
    """将标准 cron 表达式转换为 APScheduler 兼容格式。"""
    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr

    minute, hour, day, month, dow = parts
    mapping = {
        "0": "sun",
        "1": "mon",
        "2": "tue",
        "3": "wed",
        "4": "thu",
        "5": "fri",
        "6": "sat",
        "7": "sun",
    }

    def replace_func(match: re.Match[str]) -> str:
        val = match.group(0)
        return mapping.get(val, val)

    new_dow = re.sub(r"\d+", replace_func, dow)
    return f"{minute} {hour} {day} {month} {new_dow}"


def parse_recall_seconds(content_text: str) -> tuple[Optional[int], str]:
    """从提醒正文开头解析可选撤回时间，支持 [r1d]/[r2h]/[r30m]/[r10s] 组合。"""
    if not content_text:
        return None, ""

    stripped = content_text.lstrip()
    match_short = re.match(r"^\[r((?:\d+[dhms]){1,6})\](?:\s+|$)", stripped, re.IGNORECASE)
    if match_short:
        duration_expr = match_short.group(1).lower()
        duration_parts = re.findall(r"(\d+)([dhms])", duration_expr)
        recall_seconds = 0
        for val, unit in duration_parts:
            num = int(val)
            if unit == "d":
                recall_seconds += num * 86400
            elif unit == "h":
                recall_seconds += num * 3600
            elif unit == "m":
                recall_seconds += num * 60
            else:
                recall_seconds += num

        cleaned = stripped[match_short.end() :].lstrip()
        return recall_seconds, cleaned

    return None, content_text


def format_duration_cn(seconds: int) -> str:
    """将秒数格式化为中文时长。"""
    total = int(seconds or 0)
    if total <= 0:
        return "0秒"

    day = total // 86400
    hour = (total % 86400) // 3600
    minute = (total % 3600) // 60
    second = total % 60

    parts: List[str] = []
    if day > 0:
        parts.append(f"{day}天")
    if hour > 0:
        parts.append(f"{hour}小时")
    if minute > 0:
        parts.append(f"{minute}分")
    if second > 0:
        parts.append(f"{second}秒")

    return "".join(parts) if parts else "0秒"


def normalize_message_structure(message_structure: List[Dict], recall_text: str = "") -> List[Dict]:
    """将文本中的 At 标记转成消息组件，并移除已解析的撤回时间文本。"""
    normalized: List[Dict] = []
    recall_stripped = False
    mention_pattern = re.compile(
        r"\[(atall|at\s*:\s*(\d+)|@\s*(\d+)|@\s*(?:所有人|全体成员|全体|all))\]",
        re.IGNORECASE,
    )

    for comp in message_structure or []:
        if comp.get("type") != "text":
            normalized.append(comp)
            continue

        text = comp.get("content", "")
        if not recall_stripped and recall_text:
            tmp = text.lstrip()
            if tmp.startswith(recall_text):
                tmp = tmp[len(recall_text) :]
                text = tmp.lstrip()
                recall_stripped = True

        last_end = 0
        for match in mention_pattern.finditer(text):
            if match.start() > last_end:
                plain_text = text[last_end : match.start()]
                if plain_text:
                    normalized.append({"type": "text", "content": plain_text})

            qq = match.group(2) or match.group(3)
            if qq:
                normalized.append({"type": "at", "qq": qq})
            else:
                normalized.append({"type": "atall"})
            last_end = match.end()

        tail = text[last_end:]
        if tail:
            normalized.append({"type": "text", "content": tail})

    return normalized


def collect_text_from_message_structure(message_structure: List[Dict]) -> str:
    """从消息结构中拼接出纯文本内容。"""
    text_parts: List[str] = []
    for comp in message_structure or []:
        if comp.get("type") == "text":
            text_parts.append(comp.get("content", ""))
    return "".join(text_parts)


async def extract_message_structure_from_components(
    components: List[Any],
    save_media: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """从消息组件列表中提取 reminder 可持久化的消息结构。"""
    message_structure: List[Dict[str, Any]] = []
    for msg_comp in components:
        if isinstance(msg_comp, Plain):
            if msg_comp.text:
                message_structure.append({"type": "text", "content": msg_comp.text})
        elif isinstance(msg_comp, At):
            qq = str(msg_comp.qq)
            if qq.lower() == "all":
                message_structure.append({"type": "atall"})
            else:
                message_structure.append({"type": "at", "qq": qq})
        elif isinstance(msg_comp, Face):
            message_structure.append({"type": "face", "id": msg_comp.id})
        else:
            media_item = await save_media(msg_comp)
            if media_item:
                message_structure.append(media_item)
    return message_structure


async def extract_inline_message_structure(
    message_chain: List[Any],
    cron_expr: str,
    save_media: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """提取指令消息中 cron 之后的提醒内容。"""
    message_structure: List[Dict[str, Any]] = []
    cron_found = False

    for msg_comp in message_chain:
        if isinstance(msg_comp, Plain):
            if not cron_found and cron_expr in msg_comp.text:
                cron_index = msg_comp.text.index(cron_expr)
                cron_end = cron_index + len(cron_expr)
                content = msg_comp.text[cron_end:]
                cron_found = True

                if content.strip():
                    message_structure.append({"type": "text", "content": content.lstrip()})
            elif cron_found and msg_comp.text.strip():
                message_structure.append({"type": "text", "content": msg_comp.text})
        elif not cron_found:
            continue
        elif isinstance(msg_comp, At):
            qq = str(msg_comp.qq)
            if qq.lower() == "all":
                message_structure.append({"type": "atall"})
            else:
                message_structure.append({"type": "at", "qq": qq})
        elif isinstance(msg_comp, Face):
            message_structure.append({"type": "face", "id": msg_comp.id})
        else:
            media_item = await save_media(msg_comp)
            if media_item:
                message_structure.append(media_item)

    return message_structure


async def extract_message_structure_after_prefix(
    message_chain: List[Any],
    prefix_text: str,
    save_media: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """从消息链中提取前缀之后的消息结构，保留组件顺序。"""
    message_structure: List[Dict[str, Any]] = []
    remaining_prefix = prefix_text or ""

    for msg_comp in message_chain:
        if isinstance(msg_comp, Plain):
            text = msg_comp.text or ""
            if remaining_prefix:
                if text.startswith(remaining_prefix):
                    text = text[len(remaining_prefix):]
                    remaining_prefix = ""
                else:
                    max_len = min(len(text), len(remaining_prefix))
                    i = 0
                    while i < max_len and text[i] == remaining_prefix[i]:
                        i += 1
                    if i > 0:
                        text = text[i:]
                        remaining_prefix = remaining_prefix[i:]
                        if remaining_prefix and text:
                            remaining_prefix = ""
                    else:
                        # 无法匹配前缀时，避免误删内容，直接停止前缀剥离
                        remaining_prefix = ""

            if not text:
                continue
            message_structure.append({"type": "text", "content": text})
            continue

        if isinstance(msg_comp, At):
            qq = str(msg_comp.qq)
            if qq.lower() == "all":
                message_structure.append({"type": "atall"})
            else:
                message_structure.append({"type": "at", "qq": qq})
            continue

        if isinstance(msg_comp, Face):
            message_structure.append({"type": "face", "id": msg_comp.id})
            continue

        media_item = await save_media(msg_comp)
        if media_item:
            message_structure.append(media_item)

    return message_structure


async def extract_message_structure_after_token_count(
    message_chain: List[Any],
    token_count: int,
    save_media: Callable[[Any], Awaitable[Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """从消息链中移除前 N 个文本 token 后提取结构，保留组件顺序。"""
    message_structure: List[Dict[str, Any]] = []
    tokens_left = max(0, int(token_count or 0))

    def _strip_leading_tokens(text: str, count: int) -> tuple[str, int]:
        i = 0
        n = len(text)
        removed = 0
        while i < n and removed < count:
            # skip whitespace
            while i < n and text[i].isspace():
                i += 1
            if i >= n:
                break
            # consume token
            while i < n and not text[i].isspace():
                i += 1
            removed += 1
        if removed >= count:
            while i < n and text[i].isspace():
                i += 1
            return text[i:], 0
        return "", count - removed

    for msg_comp in message_chain:
        if isinstance(msg_comp, Plain):
            text = msg_comp.text or ""
            if tokens_left > 0:
                text, tokens_left = _strip_leading_tokens(text, tokens_left)
            if not text:
                continue
            message_structure.append({"type": "text", "content": text})
            continue

        if tokens_left > 0:
            continue

        if isinstance(msg_comp, At):
            qq = str(msg_comp.qq)
            if qq.lower() == "all":
                message_structure.append({"type": "atall"})
            else:
                message_structure.append({"type": "at", "qq": qq})
            continue

        if isinstance(msg_comp, Face):
            message_structure.append({"type": "face", "id": msg_comp.id})
            continue

        media_item = await save_media(msg_comp)
        if media_item:
            message_structure.append(media_item)

    return message_structure


def summarize_message_structure(message_structure: List[Dict[str, Any]]) -> Dict[str, int]:
    """统计各类消息组件数量。"""
    summary = {
        "text": 0,
        "image": 0,
        "face": 0,
        "at": 0,
        "atall": 0,
        "record": 0,
        "video": 0,
    }
    for item in message_structure or []:
        item_type = item.get("type")
        if item_type in summary:
            summary[item_type] += 1
    return summary


def extract_message_id(send_result: Any) -> Optional[int | str]:
    """尽可能从 send_message 返回值中提取 message_id。"""
    if send_result is None or isinstance(send_result, bool):
        return None

    if isinstance(send_result, (int, str)):
        return send_result

    if isinstance(send_result, dict):
        if "message_id" in send_result:
            return send_result.get("message_id")
        data = send_result.get("data")
        if isinstance(data, dict):
            return data.get("message_id")
        if "id" in send_result:
            return send_result.get("id")

    if hasattr(send_result, "message_id"):
        return getattr(send_result, "message_id")

    return None


def build_job_id(item_id: str, session: str, is_task: bool | None = None) -> str:
    """根据任务 ID 和会话构建稳定的调度任务 ID。"""
    safe_session = session.replace(":", "_")
    if is_task is None:
        return f"{item_id}::{safe_session}"
    prefix = "task" if is_task else "reminder"
    return f"{prefix}::{item_id}::{safe_session}"


def resolve_session_origin(current_origin: str, is_group_message: bool, session_param: str | None) -> str | None:
    """解析目标会话：@好友号为私聊，#群号为群聊，不传则使用当前会话。"""
    if session_param and session_param.startswith("@"):
        friend_id = session_param[1:]
        if ":" not in current_origin:
            return None
        platform = current_origin.split(":", 1)[0]
        return f"{platform}:FriendMessage:{friend_id}"

    if session_param and session_param.startswith("#"):
        group_id = session_param[1:]
        if ":" not in current_origin:
            return None
        platform = current_origin.split(":", 1)[0]
        return f"{platform}:GroupMessage:{group_id}"

    if is_group_message and ":" in current_origin:
        parts = current_origin.split(":", 1)
        if len(parts) == 2 and "GroupMessage" not in current_origin and "FriendMessage" not in current_origin:
            return f"{parts[0]}:GroupMessage:{parts[1]}"

    return current_origin


def normalize_session_param_token(token: str | None) -> str | None:
    """仅当 @/# 后为纯数字时视作会话参数。"""
    if not token or token[0] not in ("@", "#"):
        return None

    raw = token[1:]
    if raw.isdigit():
        return f"{token[0]}{raw}"

    return None


def is_user_allowed(plugin, event) -> bool:
    """统一权限检查：管理员放行；白名单为空则放行。"""
    try:
        if event.is_admin():
            return True
        whitelist = getattr(plugin, "whitelist", None)
        if whitelist is None:
            whitelist = plugin.config.get("whitelist", [])
        if not whitelist:
            return True
        return event.get_sender_id() in whitelist
    except Exception:
        return True


def describe_origin(unified_msg_origin: str) -> str:
    """将统一会话 ID 格式化为可读描述。"""
    if ":GroupMessage:" in unified_msg_origin:
        group_id = unified_msg_origin.split(":GroupMessage:", 1)[1]
        return f"👥 群聊 {group_id}"
    if ":FriendMessage:" in unified_msg_origin:
        friend_id = unified_msg_origin.split(":FriendMessage:", 1)[1]
        return f"👤 私聊 {friend_id}"
    return unified_msg_origin


def describe_target_origin(unified_msg_origin: str, current_origin: str) -> str:
    """用于新增和编辑反馈：当前会话显示为“当前会话”，否则显示具体会话。"""
    if unified_msg_origin == current_origin:
        return "当前会话"
    return describe_origin(unified_msg_origin)


def extract_reply_message_id(event: Any) -> Optional[str]:
    """从当前消息中提取被引用消息的 message_id。"""
    try:
        message_obj = getattr(event, "message_obj", None)
        segments = getattr(message_obj, "message", None) if message_obj is not None else None
        if isinstance(segments, list):
            for seg in segments:
                if seg.__class__.__name__ == "Reply":
                    seg_id = str(getattr(seg, "id", "") or getattr(seg, "message_id", "") or "").strip()
                    if seg_id:
                        return seg_id
    except Exception:
        pass

    try:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None) if message_obj is not None else None
        raw_segments = raw_message.get("message") if isinstance(raw_message, dict) else None
        if isinstance(raw_segments, list):
            for seg in raw_segments:
                if isinstance(seg, dict) and str(seg.get("type", "")).lower() == "reply":
                    data = seg.get("data", {}) or {}
                    seg_id = str(data.get("id", "") or data.get("message_id", "") or "").strip()
                    if seg_id:
                        return seg_id
    except Exception:
        pass

    text = str(getattr(event, "message_str", "") or "")
    match = re.search(r"\[CQ:reply,id=([0-9]+)\]", text, flags=re.IGNORECASE)
    if match:
        return str(match.group(1)).strip()
    return None


async def build_message_structure_from_onebot_segments(
    segments: List[Dict[str, Any]],
    save_media: Callable[[Any, str], Awaitable[Optional[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """将 get_msg 返回的 OneBot 消息段转换为 reminder message_structure。"""
    message_structure: List[Dict[str, Any]] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type", "")).lower()
        data = seg.get("data", {}) or {}

        if seg_type == "text":
            content = str(data.get("text", "") or "")
            if content:
                message_structure.append({"type": "text", "content": content})
        elif seg_type == "at":
            qq = str(data.get("qq", "") or "").strip()
            if not qq:
                continue
            if qq.lower() == "all":
                message_structure.append({"type": "atall"})
            else:
                message_structure.append({"type": "at", "qq": qq})
        elif seg_type == "face":
            face_id = data.get("id")
            if face_id is not None:
                message_structure.append({"type": "face", "id": face_id})
        elif seg_type == "image":
            source = str(data.get("url", "") or data.get("file", "") or "").strip()
            if source:
                media_item = await save_media(Image(file=source, url=data.get("url", "")), "image")
                if media_item:
                    message_structure.append(media_item)
        elif seg_type == "record":
            source = str(data.get("url", "") or data.get("file", "") or "").strip()
            if source:
                media_item = await save_media(Record(file=source, url=data.get("url", "")), "record")
                if media_item:
                    message_structure.append(media_item)
        elif seg_type == "video":
            source = str(data.get("url", "") or data.get("file", "") or "").strip()
            if source:
                media_item = await save_media(Video(file=source), "video")
                if media_item:
                    message_structure.append(media_item)

    return message_structure


async def fetch_reply_message_structure(
    event: Any,
    get_platform_adapter_name: Callable[[str], str],
    get_platform_api_client: Callable[[str], Any],
    save_media: Callable[[Any, str], Awaitable[Optional[Dict[str, Any]]]],
    logger: Any,
) -> List[Dict[str, Any]]:
    """在支持的平台上通过 get_msg 获取引用消息内容。"""
    platform_id = event.get_platform_id()
    if get_platform_adapter_name(platform_id) != "aiocqhttp":
        return []

    reply_message_id = extract_reply_message_id(event)
    if not reply_message_id or not reply_message_id.isdigit():
        return []

    api = get_platform_api_client(platform_id)
    if not api:
        return []

    try:
        original_msg = await api.call_action("get_msg", message_id=int(reply_message_id))
    except Exception as exc:
        logger.warning(f"获取引用消息失败: message_id={reply_message_id}, error={exc}")
        return []

    message_segments = original_msg.get("message") if isinstance(original_msg, dict) else None
    if not isinstance(message_segments, list):
        return []

    return await build_message_structure_from_onebot_segments(message_segments, save_media)


def build_component_from_item(msg_item: Dict[str, Any], data_dir: str, logger: Any) -> Optional[Any]:
    """从持久化的 message_structure 还原单个消息组件。"""
    item_type = msg_item.get("type")
    if item_type == "text":
        return Plain(msg_item.get("content", ""))
    if item_type == "image":
        full_path = os.path.join(data_dir, msg_item.get("path", ""))
        if os.path.exists(full_path):
            return Image.fromFileSystem(full_path)
        logger.warning(f"图片文件不存在: {full_path}")
        return None
    if item_type == "record":
        full_path = os.path.join(data_dir, msg_item.get("path", ""))
        if os.path.exists(full_path):
            return Record.fromFileSystem(full_path)
        logger.warning(f"语音文件不存在: {full_path}")
        return None
    if item_type == "video":
        full_path = os.path.join(data_dir, msg_item.get("path", ""))
        if os.path.exists(full_path):
            return Video.fromFileSystem(full_path)
        logger.warning(f"视频文件不存在: {full_path}")
        return None
    if item_type == "at":
        return At(qq=msg_item.get("qq"))
    if item_type == "atall":
        return At(qq="all")
    if item_type == "face":
        return Face(id=msg_item.get("id"))
    return None


def split_message_structure(message_structure: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """按平台限制拆分消息：video 和 record 必须单独发送。"""
    chunks: List[List[Dict[str, Any]]] = []
    current_chunk: List[Dict[str, Any]] = []

    for msg_item in message_structure or []:
        item_type = msg_item.get("type")
        if item_type in ("record", "video"):
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
            chunks.append([msg_item])
            continue
        current_chunk.append(msg_item)

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def build_message_chain_from_structure(
    message_structure: List[Dict[str, Any]],
    data_dir: str,
    logger: Any,
) -> List[Any]:
    """从 message_structure 还原一条可发送的消息链。"""
    chain: List[Any] = []

    def _needs_space_after_at(text: str) -> tuple[bool, bool, str]:
        if not text:
            return False, False, text
        first = text[0]
        if first in "\n\r":
            return False, False, text
        if first in ",.!?;:，。！？；：）)]}】》、":
            return False, False, text

        i = 0
        n = len(text)
        while i < n and text[i].isspace():
            i += 1
        if i == 0:
            return True, False, text

        leading = text[:i]
        if "\n" in leading or "\r" in leading:
            return False, True, text

        return True, True, text[i:].lstrip(" \t")

    def _inject_space_after_at(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        adjusted: List[Dict[str, Any]] = []
        i = 0
        while i < len(items):
            item = items[i]
            adjusted.append(item)
            if item.get("type") in {"at", "atall"} and i + 1 < len(items):
                nxt = items[i + 1]
                if nxt.get("type") == "text":
                    content = nxt.get("content", "")
                    need_space, had_leading, stripped = _needs_space_after_at(content)
                    if need_space:
                        if had_leading:
                            # 已有前导空格：归一化为单空格放回原文本段
                            adjusted.append({"type": "text", "content": " " + stripped})
                        else:
                            # 无前导空格：单独插入空格段，避免平台裁剪前导空格
                            adjusted.append({"type": "text", "content": " "})
                            if stripped:
                                adjusted.append({"type": "text", "content": stripped})
                        i += 2
                        continue
            i += 1
        return adjusted

    adjusted_items = _inject_space_after_at(message_structure or [])
    if message_structure != adjusted_items:
        logger.debug(
            "[reminder] build_message_chain_from_structure adjusted items: %s -> %s",
            message_structure,
            adjusted_items,
        )

    for msg_item in adjusted_items:
        component = build_component_from_item(msg_item, data_dir, logger)
        if component is not None:
            chain.append(component)
    return chain


def load_reminders(plugin):
    """从文件加载提醒数据"""
    try:
        plugin.reminders = []
        plugin.linked_tasks = {}

        if not os.path.exists(plugin.data_file):
            return

        with open(plugin.data_file, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, list):
            raw_reminders = data
            raw_linked_tasks = {}
        else:
            raw_reminders = data.get('reminders', [])
            raw_linked_tasks = data.get('linked_tasks', {})

        normalized_linked_tasks: Dict[str, List[Dict]] = {}
        for reminder_name, task_data in raw_linked_tasks.items():
            commands_list = []
            if isinstance(task_data, str):
                commands_list.append({'command': task_data, 'message_structure': []})
            elif isinstance(task_data, list):
                for cmd in task_data:
                    if isinstance(cmd, str):
                        commands_list.append({'command': cmd, 'message_structure': []})
                    elif isinstance(cmd, dict):
                        commands_list.append(cmd)
            normalized_linked_tasks[reminder_name] = commands_list

        name_map: Dict[str, str] = {}
        existing_names = set()
        need_resave = False

        for item in raw_reminders:
            orig_name = str(item.get('name', '')).strip()
            if not orig_name:
                continue

            if re.fullmatch(r"\d+", orig_name):
                prefix = "任务" if item.get('is_task', False) else "提醒"
                base_name = f"{prefix}{orig_name}"
                new_name = base_name
                suffix = 1
                while new_name in existing_names:
                    new_name = f"{base_name}_{suffix}"
                    suffix += 1
                item['name'] = new_name
                existing_names.add(new_name)
                if not item.get('is_task', False):
                    name_map[orig_name] = new_name
                need_resave = True
            else:
                if orig_name in existing_names:
                    base_name = orig_name
                    new_name = base_name
                    suffix = 1
                    while new_name in existing_names:
                        new_name = f"{base_name}_{suffix}"
                        suffix += 1
                    item['name'] = new_name
                    if not item.get('is_task', False):
                        name_map[orig_name] = new_name
                    existing_names.add(new_name)
                    need_resave = True
                else:
                    existing_names.add(orig_name)

            if 'enabled_sessions' not in item:
                unified = item.get('unified_msg_origin')
                if unified:
                    item['enabled_sessions'] = [unified]
                else:
                    item['enabled_sessions'] = []
                need_resave = True

            if 'unified_msg_origin' in item:
                item.pop('unified_msg_origin', None)
                need_resave = True

            if isinstance(item.get('message_structure'), list):
                normalized_message_structure = normalize_message_structure(item['message_structure'])
                if normalized_message_structure != item['message_structure']:
                    item['message_structure'] = normalized_message_structure
                    need_resave = True

                if item.get('is_task', False):
                    normalized_command = collect_text_from_message_structure(item['message_structure']).strip()
                    if normalized_command and normalized_command != item.get('command', ''):
                        item['command'] = normalized_command
                        need_resave = True

            plugin.reminders.append(item)

        migrated_linked_tasks: Dict[str, List[Dict]] = {}
        for old_name, commands in normalized_linked_tasks.items():
            new_name = name_map.get(old_name, old_name)
            normalized_commands: List[Dict] = []
            for cmd in commands:
                if isinstance(cmd, str):
                    normalized_commands.append({'command': cmd, 'message_structure': []})
                    need_resave = True
                    continue

                if not isinstance(cmd, dict):
                    need_resave = True
                    continue

                cmd_struct = cmd.get('message_structure')
                if not isinstance(cmd_struct, list):
                    cmd_struct = []
                    cmd['message_structure'] = cmd_struct
                    need_resave = True

                normalized_cmd_struct = normalize_message_structure(cmd_struct)
                if normalized_cmd_struct != cmd_struct:
                    cmd['message_structure'] = normalized_cmd_struct
                    need_resave = True

                normalized_commands.append(cmd)

            if new_name not in migrated_linked_tasks:
                migrated_linked_tasks[new_name] = normalized_commands
            else:
                migrated_linked_tasks[new_name].extend(normalized_commands)

        plugin.linked_tasks = migrated_linked_tasks

        if name_map or need_resave:
            save_reminders(plugin)
    except Exception as e:
        logger.error(f"加载数据失败: {e}", exc_info=True)
        plugin.reminders = []
        plugin.linked_tasks = {}


def save_reminders(plugin):
    """保存提醒数据到文件"""
    try:
        data = {
            'reminders': plugin.reminders,
            'linked_tasks': plugin.linked_tasks
        }

        with open(plugin.data_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.debug("数据已保存")
    except Exception as e:
        logger.error(f"保存数据失败: {e}", exc_info=True)


async def restore_reminders(plugin):
    """恢复提醒任务"""
    try:
        if not plugin.reminders:
            return

        count = 0
        for item in plugin.reminders:
            try:
                # 兼容旧数据结构
                if 'cron_expr' not in item and 'cron' in item:
                    item['cron_expr'] = item['cron']
                if 'sessions' in item and 'enabled_sessions' not in item:
                    item['enabled_sessions'] = item['sessions']

                # 恢复调度
                await schedule_reminder(plugin, item)
                count += 1
            except Exception as e:
                logger.error(f"恢复任务 {item.get('name')} 失败: {e}")

        logger.info(f"已恢复 {count} 个任务/提醒")
    except Exception as e:
        logger.error(f"恢复数据失败: {e}", exc_info=True)


def add_job(plugin, item: Dict, session: str):
    """添加任务到调度器"""
    try:
        job_id = build_job_id(item.get('name', ''), session, is_task=item.get('is_task', False))
        legacy_job_id = build_job_id(item.get('name', ''), session, is_task=None)
        cron_expr = item.get('cron_expr', item.get('cron', ''))
        aps_cron = translate_to_apscheduler_cron(cron_expr)
        trigger = CronTrigger.from_crontab(aps_cron)

        # 如果已存在相同ID的任务，先删除
        for jid in {job_id, legacy_job_id}:
            try:
                plugin.scheduler.remove_job(jid)
            except Exception:
                pass  # 任务不存在，正常情况

        plugin.scheduler.add_job(
            trigger_reminder,
            trigger=trigger,
            id=job_id,
            args=[plugin, item, session]
        )

        # 保存任务映射
        if not hasattr(plugin, 'job_mapping'):
            plugin.job_mapping = {}
        plugin.job_mapping[job_id] = (item, session)

    except Exception as e:
        logger.error(f"添加调度任务失败: {e}", exc_info=True)
        raise


def remove_job(plugin, item: Dict, session: str):
    """从调度器移除任务"""
    try:
        job_id = build_job_id(item.get('name', ''), session, is_task=item.get('is_task', False))
        legacy_job_id = build_job_id(item.get('name', ''), session, is_task=None)

        # 修复：直接尝试删除任务，如果不存在会抛出异常
        removed = False
        for jid in {job_id, legacy_job_id}:
            try:
                plugin.scheduler.remove_job(jid)
                logger.info(f"已移除调度任务: {jid}, 会话: {session}")
                removed = True
            except Exception:
                # 任务不存在时的正常情况，静默处理
                pass
        if not removed:
            logger.debug(f"调度任务不存在，跳过移除: {job_id}, 会话: {session}")

        # 清理任务映射
        if hasattr(plugin, 'job_mapping') and job_id in plugin.job_mapping:
            del plugin.job_mapping[job_id]
    except Exception as e:
        logger.error(f"移除调度任务失败: {e}", exc_info=True)


def remove_all_jobs_for_item(plugin, item: Dict):
    """移除某个任务在所有会话中的调度"""
    try:
        # 尝试移除可能存在的多个任务实例（基于映射）
        if hasattr(plugin, 'job_mapping'):
            jobs_to_remove = [
                jid for jid, (itm, _) in list(plugin.job_mapping.items())
                if itm.get('name') == item.get('name') and
                   itm.get('is_task', False) == item.get('is_task', False)
            ]

            for jid in jobs_to_remove:
                try:
                    plugin.scheduler.remove_job(jid)
                    logger.info(f"已移除调度任务: {jid}")
                except Exception:
                    # 任务不存在时的正常情况，静默处理
                    pass
                if jid in plugin.job_mapping:
                    del plugin.job_mapping[jid]

        # 补充清理（兼容历史/不同ID格式）
        for session in item.get('enabled_sessions', []) or []:
            for jid in {
                build_job_id(item.get('name', ''), session, is_task=item.get('is_task', False)),
                build_job_id(item.get('name', ''), session, is_task=None),
            }:
                try:
                    plugin.scheduler.remove_job(jid)
                except Exception:
                    pass
                if hasattr(plugin, 'job_mapping') and jid in plugin.job_mapping:
                    del plugin.job_mapping[jid]

        logger.info(f"已移除任务的所有调度: {item.get('name')}")
    except Exception as e:
        logger.error(f"移除任务调度失败: {e}", exc_info=True)


async def schedule_reminder(plugin, item: Dict):
    """调度提醒或任务"""
    try:
        sessions = item.get('enabled_sessions', [])
        if not sessions:
            logger.warning(f"任务/提醒 '{item.get('name')}' 没有启用的会话，跳过调度")
            return

        for session in sessions:
            add_job(plugin, item, session)

    except Exception as e:
        logger.error(f"调度任务/提醒失败: {e}", exc_info=True)
        raise


async def reschedule_reminder(plugin, item: Dict):
    """重新调度提醒或任务"""
    try:
        # 移除旧调度
        remove_all_jobs_for_item(plugin, item)

        # 添加新调度
        await schedule_reminder(plugin, item)

        logger.info(f"已重新调度任务/提醒 '{item.get('name')}'")
    except Exception as e:
        logger.error(f"重新调度任务/提醒失败: {e}", exc_info=True)
        raise


def get_platform_adapter_name(plugin, platform_id: str) -> str:
    """将 unified_msg_origin 里的平台实例ID解析为适配器名（如 aiocqhttp）。"""
    if not platform_id:
        return ""

    platform_inst = plugin.context.get_platform_inst(platform_id)
    if not platform_inst:
        return platform_id

    try:
        if hasattr(platform_inst, 'meta'):
            meta = platform_inst.meta()
            if hasattr(meta, 'name') and meta.name:
                return str(meta.name)
    except Exception:
        pass

    return platform_id


def get_platform_api_client(plugin, platform_id: str):
    """获取平台 API 客户端"""
    try:
        platform_inst = plugin.context.get_platform_inst(platform_id)
        if not platform_inst:
            return None

        if hasattr(platform_inst, 'bot') and hasattr(platform_inst.bot, 'api'):
            return platform_inst.bot.api
        if hasattr(platform_inst, 'client') and hasattr(platform_inst.client, 'api'):
            return platform_inst.client.api
        if hasattr(platform_inst, 'get_client'):
            client = platform_inst.get_client()
            if hasattr(client, 'api'):
                return client.api
        return None
    except Exception as e:
        logger.error(f"获取平台 API 客户端失败: {e}")
        return None


def check_recall_capability(plugin, unified_msg_origin: str) -> tuple[bool, str]:
    """检查平台是否支持撤回"""
    try:
        if not unified_msg_origin or ':' not in unified_msg_origin:
            return False, "无法识别目标会话的平台信息"

        platform_id = unified_msg_origin.split(':', 1)[0]
        adapter_name = get_platform_adapter_name(plugin, platform_id)
        if adapter_name != 'aiocqhttp':
            return False, f"平台 {platform_id}({adapter_name}) 暂不支持自动撤回"

        api = get_platform_api_client(plugin, platform_id)
        if not api:
            return False, f"平台 {platform_id} 缺少可用 API，无法自动撤回"

        return True, ""

    except Exception as e:
        logger.error(f"检查撤回能力失败: {e}")
        return False, f"检查失败: {e}"


async def send_aiocqhttp_with_message_id(
    plugin, item: Dict, unified_msg_origin: str | None = None
) -> Optional[int | str]:
    """通过 OneBot v11 直接发送消息，确保拿到 message_id。"""
    if not unified_msg_origin:
        unified_msg_origin = item.get("unified_msg_origin") or item.get("session")

    if not unified_msg_origin or ':' not in unified_msg_origin:
        return None

    parts = unified_msg_origin.split(':', 2)
    if len(parts) < 3:
        return None

    platform_id, msg_type, target_id = parts[0], parts[1], parts[2]
    if get_platform_adapter_name(plugin, platform_id) != 'aiocqhttp':
        return None

    api = get_platform_api_client(plugin, platform_id)
    if not api:
        return None

    segments = []
    for msg_item in item.get('message_structure', []):
        mtype = msg_item.get('type')
        if mtype == 'text':
            segments.append({'type': 'text', 'data': {'text': msg_item.get('content', '')}})
        elif mtype == 'at':
            segments.append({'type': 'at', 'data': {'qq': str(msg_item.get('qq', ''))}})
        elif mtype == 'atall':
            segments.append({'type': 'at', 'data': {'qq': 'all'}})
        elif mtype == 'face':
            segments.append({'type': 'face', 'data': {'id': msg_item.get('id')}})
        elif mtype == 'image':
            full_path = os.path.join(plugin.data_dir, msg_item.get('path', ''))
            if os.path.exists(full_path):
                try:
                    file_uri = Path(full_path).resolve().as_uri()
                except Exception:
                    file_uri = full_path
                segments.append({'type': 'image', 'data': {'file': file_uri}})
        elif mtype == 'record':
            full_path = os.path.join(plugin.data_dir, msg_item.get('path', ''))
            if os.path.exists(full_path):
                try:
                    file_uri = Path(full_path).resolve().as_uri()
                except Exception:
                    file_uri = full_path
                segments.append({'type': 'record', 'data': {'file': file_uri}})
        elif mtype == 'video':
            full_path = os.path.join(plugin.data_dir, msg_item.get('path', ''))
            if os.path.exists(full_path):
                try:
                    file_uri = Path(full_path).resolve().as_uri()
                except Exception:
                    file_uri = full_path
                segments.append({'type': 'video', 'data': {'file': file_uri}})

    if not segments:
        return None

    if msg_type == 'GroupMessage':
        ret = await api.call_action('send_group_msg', group_id=int(target_id), message=segments)
    elif msg_type == 'FriendMessage':
        ret = await api.call_action('send_private_msg', user_id=int(target_id), message=segments)
    else:
        return None

    return extract_message_id(ret)


async def recall_message_later(plugin, unified_msg_origin: str, message_id: int | str, delay_seconds: int):
    """延时撤回消息，目前仅在 aiocqhttp(OneBot v11) 平台启用。"""
    if delay_seconds <= 0:
        return

    try:
        await asyncio.sleep(delay_seconds)

        platform_id = unified_msg_origin.split(':', 1)[0] if ':' in unified_msg_origin else ""
        adapter_name = get_platform_adapter_name(plugin, platform_id)
        if adapter_name != 'aiocqhttp':
            logger.info(f"平台 {platform_id}({adapter_name}) 暂不支持自动撤回，跳过 message_id={message_id}")
            return

        api = get_platform_api_client(plugin, platform_id)
        if not api:
            logger.warning(f"平台实例缺少可用 API，无法撤回消息: {platform_id}")
            return

        ret = await api.call_action('delete_msg', message_id=message_id)
        logger.info(f"已撤回消息 message_id={message_id}, ret={ret}")
    except Exception as e:
        logger.error(f"自动撤回消息失败 message_id={message_id}: {e}", exc_info=True)


async def save_media_component(plugin, msg_comp: Image | Record | Video, prefix: str) -> Optional[Dict[str, str]]:
    """保存媒体组件到本地"""
    try:
        source_path = await msg_comp.convert_to_file_path()
        suffix = Path(source_path).suffix or {
            'image': '.jpg',
            'record': '.amr',
            'video': '.mp4',
        }.get(prefix, '.bin')

        filename = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(source_path.encode()).hexdigest()[:8]}{suffix}"
        filepath = os.path.join(plugin.data_dir, filename)
        shutil.copyfile(source_path, filepath)

        return {'type': prefix, 'path': filename}

    except Exception as e:
        logger.error(f"保存媒体组件失败: {e}", exc_info=True)
        return None


async def trigger_reminder(plugin, item: Dict, session: str):
    """触发提醒或任务"""
    try:
        if item.get('is_task', False):
            # 执行任务
            await execute_task(plugin, item, session)
        else:
            # 发送提醒
            await send_reminder(plugin, item, session)

    except Exception as e:
        logger.error(f"触发任务/提醒失败: {e}", exc_info=True)


async def send_reminder(plugin, item: Dict, session: str):
    """发送定时提醒"""
    try:
        message_structure = item.get('message_structure', [])
        if not message_structure:
            logger.warning(f"提醒 '{item.get('name')}' 没有内容，跳过发送")
            return

        # 按平台限制拆分消息
        from .utils import split_message_structure, build_message_chain_from_structure, extract_message_id
        message_chunks = split_message_structure(message_structure)

        if not message_chunks:
            logger.warning(f"提醒 '{item.get('name')}' 消息为空")
            return

        recall_after_seconds = int(item.get('recall_after_seconds', 0) or 0)
        message_ids = []

        # 检查撤回支持
        recall_supported = False
        recall_reason = ""
        if recall_after_seconds > 0:
            recall_supported, recall_reason = check_recall_capability(plugin, session)
            if not recall_supported:
                logger.warning(
                    f"提醒 '{item.get('name', 'unknown')}' 配置了自动撤回，但{recall_reason}，本次仅发送不撤回")

        for chunk in message_chunks:
            chain = build_message_chain_from_structure(
                chunk,
                plugin.data_dir,
                logger
            )

            if not chain:
                continue

            message_id = None
            # 如果需要撤回且平台支持，使用 aiocqhttp API 发送
            if recall_after_seconds > 0 and recall_supported:
                message_id = await send_aiocqhttp_with_message_id(
                    plugin,
                    {'message_structure': chunk},
                    session,
                )

            # 如果 aiocqhttp 发送失败或不支持撤回，使用普通方式发送
            if message_id is None:
                message_chain = MessageChain()
                message_chain.chain = chain
                send_ret = await plugin.context.send_message(session, message_chain)
                message_id = extract_message_id(send_ret)

            if message_id is not None:
                message_ids.append(message_id)

        # 处理撤回
        if recall_after_seconds > 0 and recall_supported:
            if message_ids:
                for message_id in message_ids:
                    asyncio.create_task(
                        recall_message_later(plugin, session, message_id, recall_after_seconds))
            else:
                logger.warning(f"提醒已发送但未拿到 message_id，无法自动撤回: {item.get('name')}")

        logger.info(f"提醒 '{item.get('name')}' 已发送到 {session}")

        # 处理链接任务
        linked_commands = plugin.linked_tasks.get(item.get('name'), [])
        if linked_commands:
            logger.info(f"提醒 '{item.get('name')}' 有 {len(linked_commands)} 个链接任务，开始执行")
            from .reminder_manager import ReminderManager
            reminder_manager = ReminderManager(plugin)
            tasks = []
            for linked_command in linked_commands:
                task = asyncio.create_task(reminder_manager._execute_linked_command(linked_command, session, item))
                tasks.append(task)

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info(f"提醒 '{item.get('name')}' 的所有链接任务执行完成")

    except Exception as e:
        logger.error(f"发送提醒 '{item.get('name')}' 时出错: {e}", exc_info=True)


async def handle_recall(plugin, item: Dict, session: str, delay_seconds: int):
    """处理消息撤回"""
    try:
        # 检查平台是否支持撤回
        recall_ok, reason = check_recall_capability(plugin, session)
        if not recall_ok:
            if session not in plugin._recall_notice_sent:
                plugin._recall_notice_sent.add(session)
                logger.info(f"会话 {session} 不支持自动撤回: {reason}")
            return

        # 发送消息并获取消息ID
        message_id = await send_aiocqhttp_with_message_id(plugin, item, session)
        if message_id:
            # 延迟撤回
            await recall_message_later(plugin, session, message_id, delay_seconds)

    except Exception as e:
        logger.error(f"处理撤回时出错: {e}", exc_info=True)


async def execute_linked_command(plugin, command: str, unified_msg_origin: str, item: Dict,
                                original_components: list = None, is_admin: bool = True,
                                self_id: str = None):
    """执行链接命令的通用函数"""
    try:
        from .event_factory import EventFactory
        from astrbot.api.message_components import At, Face

        logger.info(f"执行链接任务: {command}")

        # 如果没有传入组件，从 command 文本中解析组件（兼容历史数据）
        if not original_components:
            original_components = []
            from .utils import normalize_message_structure
            cmd_structure = normalize_message_structure([{'type': 'text', 'content': command}])
            for comp in cmd_structure:
                if comp.get('type') == 'at':
                    original_components.append(At(qq=comp.get('qq', '')))
                elif comp.get('type') == 'atall':
                    original_components.append(At(qq="all"))
                elif comp.get('type') == 'face':
                    original_components.append(Face(id=comp.get('id')))

        # 创建事件并派发
        event_factory = EventFactory(plugin.context)
        event = event_factory.create_event(
            unified_msg_origin,
            command,
            item.get('created_by', 'timer'),
            item.get('creator_name', 'Timer'),
            original_components=original_components or [],
            is_admin=is_admin,
            self_id=self_id
        )

        try:
            event.set_extra("reminder_timer_origin", True)
        except Exception:
            pass

        # 派发事件
        plugin.context.get_event_queue().put_nowait(event)
        logger.info(f"链接任务已派发: {command}")

    except Exception as e:
        logger.error(f"执行链接命令失败: {e}", exc_info=True)


async def execute_task(plugin, item: Dict, session: str):
    """执行定时任务"""
    try:
        from .event_factory import EventFactory
        from astrbot.api.message_components import At, Face

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

        # 创建事件并派发
        event_factory = EventFactory(plugin.context)
        event = event_factory.create_event(
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
        plugin.context.get_event_queue().put_nowait(event)
        logger.info(f"任务 '{item.get('name')}' 已派发: {command}")

    except Exception as e:
        logger.error(f"执行任务失败: {item.get('name', 'unknown')}, {e}", exc_info=True)
