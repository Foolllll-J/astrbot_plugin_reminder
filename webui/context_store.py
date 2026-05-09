import datetime
import json
from pathlib import Path
from typing import Any


def build_context_from_event(event: Any) -> dict[str, Any]:
    return {
        "created_by": str(event.get_sender_id() or "").strip(),
        "creator_name": str(event.get_sender_name() or "").strip(),
        "is_admin": bool(event.is_admin()),
        "self_id": str(event.get_self_id() or "").strip(),
        "source_origin": str(getattr(event, "unified_msg_origin", "") or "").strip(),
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_webui_context(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def load_webui_context(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None

    try:
        with target.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    normalized = {
        "created_by": str(payload.get("created_by", "") or "").strip(),
        "creator_name": str(payload.get("creator_name", "") or "").strip(),
        "is_admin": bool(payload.get("is_admin", False)),
        "self_id": str(payload.get("self_id", "") or "").strip(),
        "source_origin": str(payload.get("source_origin", "") or "").strip(),
        "updated_at": str(payload.get("updated_at", "") or "").strip(),
    }

    if not normalized["created_by"] or not normalized["source_origin"]:
        return None
    return normalized


def describe_context(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not payload:
        return None

    return {
        "created_by": str(payload.get("created_by", "") or "").strip(),
        "creator_name": str(payload.get("creator_name", "") or "").strip(),
        "is_admin": bool(payload.get("is_admin", False)),
        "self_id": str(payload.get("self_id", "") or "").strip(),
        "source_origin": str(payload.get("source_origin", "") or "").strip(),
        "updated_at": str(payload.get("updated_at", "") or "").strip(),
    }
