import re
from typing import Any, Dict, List, Optional


def translate_to_apscheduler_cron(cron_expr: str) -> str:
    """将标准 cron 表达式（0-7, 0/7=Sun）转换为 APScheduler 兼容格式。"""
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

    # [r1d] / [r2h] / [r30m] / [r10s] / [r1d2h30m]
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
    """将秒数格式化为中文时长（如 5秒、1分30秒、2小时1分、1天2小时）。"""
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
    """将 [atall]/[at:QQ号]/[@QQ号]/[@所有人] 标识转成消息组件，并移除已解析的撤回时间文本。"""
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
    """从消息结构中拼接并返回纯文本内容。"""
    text_parts: List[str] = []
    for comp in message_structure or []:
        if comp.get("type") == "text":
            text_parts.append(comp.get("content", ""))
    return "".join(text_parts)


def extract_message_id(send_result: Any) -> Optional[int | str]:
    """尽最大可能从 send_message 返回值中提取 message_id。"""
    if send_result is None:
        return None

    if isinstance(send_result, bool):
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
    """根据任务ID和会话构建稳定的调度任务ID。"""
    safe_session = session.replace(":", "_")
    return f"{item_id}::{safe_session}"


def resolve_session_origin(current_origin: str, is_group_message: bool, session_param: str | None) -> str | None:
    """解析目标会话：@好友号 -> FriendMessage，#群号 -> GroupMessage，无参数则使用当前会话。"""
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
    """把统一会话ID格式化为可读描述。"""
    if ":GroupMessage:" in unified_msg_origin:
        group_id = unified_msg_origin.split(":GroupMessage:", 1)[1]
        return f"群聊 {group_id}"
    if ":FriendMessage:" in unified_msg_origin:
        friend_id = unified_msg_origin.split(":FriendMessage:", 1)[1]
        return f"私聊 {friend_id}"
    return unified_msg_origin


def describe_target_origin(unified_msg_origin: str, current_origin: str) -> str:
    """用于新增/编辑反馈：当前会话显示“当前会话”，否则显示具体会话。"""
    if unified_msg_origin == current_origin:
        return "当前会话"
    return describe_origin(unified_msg_origin)
