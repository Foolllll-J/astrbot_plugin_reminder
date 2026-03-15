import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, Dict, List, Optional


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
    from astrbot.api.message_components import At, Face, Plain

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
    from astrbot.api.message_components import At, Face, Plain

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
                    message_structure.append({"type": "text", "content": content})
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


def build_job_id(item_id: str, session: str) -> str:
    """根据任务 ID 和会话构建稳定的调度任务 ID。"""
    safe_session = session.replace(":", "_")
    return f"{item_id}::{safe_session}"


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


def describe_origin(unified_msg_origin: str) -> str:
    """将统一会话 ID 格式化为可读描述。"""
    if ":GroupMessage:" in unified_msg_origin:
        group_id = unified_msg_origin.split(":GroupMessage:", 1)[1]
        return f"群聊 {group_id}"
    if ":FriendMessage:" in unified_msg_origin:
        friend_id = unified_msg_origin.split(":FriendMessage:", 1)[1]
        return f"私聊 {friend_id}"
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
    from astrbot.api.message_components import Image, Record, Video

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
    from astrbot.api.message_components import At, Face, Image, Plain, Record, Video

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
    for msg_item in message_structure or []:
        component = build_component_from_item(msg_item, data_dir, logger)
        if component is not None:
            chain.append(component)
    return chain
