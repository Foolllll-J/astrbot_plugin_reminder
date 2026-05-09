import re
from typing import Any


def build_cron_expr(payload: dict[str, Any]) -> str:
    parts = [
        str(payload.get("minute", "")).strip(),
        str(payload.get("hour", "")).strip(),
        str(payload.get("day", "")).strip(),
        str(payload.get("month", "")).strip(),
        str(payload.get("weekday", "")).strip(),
    ]
    cron_expr = " ".join(parts)
    if len(cron_expr.split()) != 5:
        raise ValueError("cron 需要 5 段")
    return cron_expr


def split_cron_expr(cron_expr: str) -> dict[str, str]:
    parts = (cron_expr or "").split()
    if len(parts) != 5:
        return {
            "minute": "",
            "hour": "",
            "day": "",
            "month": "",
            "weekday": "",
        }
    return {
        "minute": parts[0],
        "hour": parts[1],
        "day": parts[2],
        "month": parts[3],
        "weekday": parts[4],
    }


def build_recall_seconds(payload: dict[str, Any]) -> int | None:
    if not payload or not payload.get("enabled"):
        return None

    days = max(0, int(payload.get("days", 0) or 0))
    hours = max(0, int(payload.get("hours", 0) or 0))
    minutes = max(0, int(payload.get("minutes", 0) or 0))
    seconds = max(0, int(payload.get("seconds", 0) or 0))
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def split_recall_seconds(total_seconds: int | None) -> dict[str, Any]:
    total = int(total_seconds or 0)
    if total <= 0:
        return {
            "enabled": False,
            "days": 0,
            "hours": 0,
            "minutes": 0,
            "seconds": 0,
        }

    days = total // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return {
        "enabled": True,
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
    }


def build_execution_fields(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("mode", "unlimited")).strip()
    if mode != "limited":
        return {"max_executions": None}

    count = max(0, int(payload.get("count", 0) or 0))
    return {
        "max_executions": count if count > 0 else None,
        "executed_count": 0,
    }


def split_execution_fields(item: dict[str, Any]) -> dict[str, Any]:
    max_executions = item.get("max_executions")
    if max_executions is None:
        return {"mode": "unlimited", "count": 0}

    try:
        count = int(max_executions or 0)
    except (TypeError, ValueError):
        count = 0

    if count <= 0:
        return {"mode": "unlimited", "count": 0}
    return {"mode": "limited", "count": count}


def normalize_sessions(
    payload: list[dict[str, Any]] | None,
    *,
    platform: str = "aiocqhttp",
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()

    for item in payload or []:
        raw_origin = str(item.get("origin", "")).strip()
        if (
            raw_origin
            and ":GroupMessage:" in raw_origin
            and raw_origin.split(":GroupMessage:", 1)[1].isdigit()
        ) or (
            raw_origin
            and ":FriendMessage:" in raw_origin
            and raw_origin.split(":FriendMessage:", 1)[1].isdigit()
        ):
            if raw_origin not in seen:
                seen.add(raw_origin)
                normalized.append(raw_origin)
            continue

        kind = str(item.get("kind", "")).strip().lower()
        target_id = str(item.get("target_id", "")).strip()
        if not target_id.isdigit():
            continue

        msg_type = ""
        if kind == "group":
            msg_type = "GroupMessage"
        elif kind == "private":
            msg_type = "FriendMessage"
        else:
            continue

        origin = f"{platform}:{msg_type}:{target_id}"
        if origin in seen:
            continue
        seen.add(origin)
        normalized.append(origin)

    return normalized


def serialize_sessions(enabled_sessions: list[str] | None) -> list[dict[str, str]]:
    sessions: list[dict[str, str]] = []
    for origin in enabled_sessions or []:
        if ":GroupMessage:" in origin:
            target_id = origin.split(":GroupMessage:", 1)[1]
            sessions.append(
                {
                    "kind": "group",
                    "target_id": target_id,
                    "origin": origin,
                    "label": f"群聊 {target_id}",
                    "raw_origin": origin,
                }
            )
        elif ":FriendMessage:" in origin:
            target_id = origin.split(":FriendMessage:", 1)[1]
            sessions.append(
                {
                    "kind": "private",
                    "target_id": target_id,
                    "origin": origin,
                    "label": f"私聊 {target_id}",
                    "raw_origin": origin,
                }
            )
    return sessions


def sanitize_message_structure(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in payload or []:
        item_type = str(item.get("type", "")).strip().lower()
        if item_type == "text":
            content = str(item.get("content", ""))
            if content:
                sanitized.append({"type": "text", "content": content})
        elif item_type == "at":
            qq = str(item.get("qq", "")).strip()
            if qq.isdigit():
                sanitized.append({"type": "at", "qq": qq})
        elif item_type == "atall":
            sanitized.append({"type": "atall"})
        elif item_type == "face":
            try:
                face_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            sanitized.append({"type": "face", "id": face_id})
        elif item_type in {"image", "record", "video"}:
            rel_path = str(item.get("path", "")).strip()
            if rel_path and not rel_path.startswith(("..", "/", "\\")):
                sanitized.append({"type": item_type, "path": rel_path})
    return sanitized


def sanitize_linked_commands(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    linked: list[dict[str, Any]] = []
    for item in payload or []:
        command = str(item.get("command", "")).strip()
        if not command:
            continue
        linked.append(
            {
                "command": command,
                "message_structure": sanitize_message_structure(
                    item.get("message_structure")
                ),
            }
        )
    return linked


def sanitize_task_commands(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for item in payload or []:
        command = str(item.get("command", "")).strip()
        if not command:
            continue
        commands.append({"command": command})
    return commands


def serialize_item(item: dict[str, Any], linked_commands: list[dict[str, Any]] | None) -> dict[str, Any]:
    cron_expr = item.get("cron_expr", item.get("cron", ""))
    payload = {
        "name": item.get("name", ""),
        "is_task": bool(item.get("is_task", False)),
        "is_paused": bool(item.get("is_paused", False)),
        "cron_expr": cron_expr,
        "cron_fields": split_cron_expr(cron_expr),
        "enabled_sessions": serialize_sessions(item.get("enabled_sessions", [])),
        "execution": split_execution_fields(item),
        "executed_count": int(item.get("executed_count", 0) or 0),
        "created_at": item.get("created_at", ""),
        "created_by": item.get("created_by", ""),
        "creator_name": item.get("creator_name", ""),
        "is_admin": item.get("is_admin", True),
        "self_id": item.get("self_id", ""),
    }

    if payload["is_task"]:
        payload["command"] = item.get("command", "")
        payload["commands"] = sanitize_task_commands(item.get("commands", []))
        payload["message_structure"] = sanitize_message_structure(
            item.get("message_structure", [])
        )
    else:
        payload["recall"] = split_recall_seconds(item.get("recall_after_seconds"))
        payload["message_structure"] = sanitize_message_structure(
            item.get("message_structure", [])
        )
        payload["linked_commands"] = sanitize_linked_commands(linked_commands)

    return payload


def validate_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("名称不能为空")
    if re.fullmatch(r"\d+", normalized):
        raise ValueError("名称不能为纯数字")
    return normalized
