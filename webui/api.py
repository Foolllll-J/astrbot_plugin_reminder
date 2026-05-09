import datetime
import hashlib
import os
import traceback
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools
from quart import jsonify, request

from ..core.linked_task_manager import LinkedTaskManager
from ..core.reminder_manager import ReminderManager
from ..core.task_manager import TaskManager
from ..core.utils import (
    apply_execution_limit,
    execute_task,
    save_reminders,
    send_reminder,
    translate_to_apscheduler_cron,
)
from .payloads import (
    build_cron_expr,
    build_execution_fields,
    build_recall_seconds,
    normalize_sessions,
    sanitize_linked_commands,
    sanitize_task_commands,
    sanitize_message_structure,
    serialize_item,
    validate_name,
)
from .context_store import describe_context

try:
    from apscheduler.triggers.cron import CronTrigger
except Exception:  # pragma: no cover - runtime dependency guard
    CronTrigger = None


PLUGIN_NAME = "astrbot_plugin_reminder"


class ReminderWebUIApi:
    def __init__(self, plugin):
        self.plugin = plugin

    def register(self) -> None:
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/state",
            self._wrap_handler(self.get_state),
            ["GET"],
            "Get reminder console state",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/save",
            self._wrap_handler(self.save_item),
            ["POST"],
            "Create or update reminder/task",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/delete",
            self._wrap_handler(self.delete_item),
            ["POST"],
            "Delete reminder/task",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/session-toggle",
            self._wrap_handler(self.toggle_session),
            ["POST"],
            "Enable or disable one session for item",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/toggle-active",
            self._wrap_handler(self.toggle_active),
            ["POST"],
            "Pause or resume one reminder/task",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/trigger-now",
            self._wrap_handler(self.trigger_now),
            ["POST"],
            "Trigger reminder/task immediately",
        )
        self.plugin.context.register_web_api(
            f"/{PLUGIN_NAME}/upload-media/<component_type>",
            self._wrap_handler(self.upload_media),
            ["POST"],
            "Upload reminder media asset",
        )

    def _wrap_handler(self, handler):
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except ValueError as exc:
                return jsonify(
                    {
                        "status": "error",
                        "message": str(exc),
                    }
                )
            except Exception as exc:  # pragma: no cover - runtime guard
                logger.error(
                    "Reminder WebUI API failed in %s: %s\n%s",
                    getattr(handler, "__name__", "unknown"),
                    exc,
                    traceback.format_exc(),
                )
                return jsonify(
                    {
                        "status": "error",
                        "message": "操作失败，请检查输入后重试。",
                    }
                )

        return wrapped

    async def get_state(self):
        reminders = []
        tasks = []
        for item in self.plugin.reminders:
            linked_commands = None
            if not item.get("is_task", False):
                linked_commands = self.plugin.linked_tasks.get(item.get("name", ""), [])
            serialized = serialize_item(item, linked_commands)
            if item.get("is_task", False):
                tasks.append(serialized)
            else:
                reminders.append(serialized)

        return jsonify(
            {
                "reminders": reminders,
                "tasks": tasks,
                "webui_context_ready": bool(self.plugin.webui_context),
                "webui_context": describe_context(self.plugin.webui_context),
                "stats": {
                    "reminders": len(reminders),
                    "tasks": len(tasks),
                    "links": sum(len(v) for v in self.plugin.linked_tasks.values()),
                },
            }
        )

    async def save_item(self):
        context_payload = self._require_webui_context()
        payload = await request.get_json(force=True)
        is_task = bool(payload.get("is_task", False))
        original_name = str(payload.get("original_name", "")).strip()
        name = validate_name(payload.get("name", ""))

        existing_item = self._find_item(original_name or name, is_task)
        if existing_item is None and original_name:
            existing_item = self._find_any_item(original_name)
        if existing_item is None:
            self._ensure_name_available(name, is_task)
            item = self._build_new_item(name, is_task)
            is_new = True
        else:
            if original_name and original_name != name:
                self._ensure_name_available(name, is_task, exclude_name=original_name)
            item = existing_item
            is_new = False

        item["name"] = name
        item["is_paused"] = bool(payload.get("is_paused", item.get("is_paused", False)))
        cron_expr = str(payload.get("cron_expr", "")).strip() or build_cron_expr(
            payload.get("cron_fields", {})
        )
        self._validate_cron(cron_expr)
        item["cron_expr"] = cron_expr

        platform = self._resolve_platform(item)
        enabled_sessions = normalize_sessions(payload.get("enabled_sessions"), platform=platform)
        if not enabled_sessions:
            raise ValueError("至少需要一个目标会话")
        item["enabled_sessions"] = enabled_sessions
        self._apply_webui_context(item, context_payload)

        execution_fields = build_execution_fields(payload.get("execution", {}))
        limit_changed = False
        if execution_fields.get("max_executions") is None:
            if item.get("max_executions") is not None or "executed_count" in item:
                item["max_executions"] = None
                item.pop("executed_count", None)
                limit_changed = True
        else:
            limit_changed = apply_execution_limit(item, execution_fields["max_executions"])

        if is_task:
            if not item.get("is_task", False):
                self.plugin.linked_tasks.pop(original_name or name, None)
            commands = sanitize_task_commands(payload.get("commands"))
            command = str(payload.get("command", "")).strip()
            if not commands and command:
                commands = [{"command": command}]
            if not commands:
                raise ValueError("任务命令不能为空")
            item["is_task"] = True
            item["commands"] = commands
            item["command"] = commands[0]["command"]
            item["message_structure"] = sanitize_message_structure(
                payload.get("message_structure")
            )
            item.pop("recall_after_seconds", None)
        else:
            item["is_task"] = False
            item.pop("commands", None)
            item.pop("command", None)
            item["message_structure"] = sanitize_message_structure(
                payload.get("message_structure")
            )
            if not item["message_structure"]:
                raise ValueError("提醒内容不能为空")

            recall_seconds = build_recall_seconds(payload.get("recall", {}))
            if recall_seconds is None:
                item.pop("recall_after_seconds", None)
            else:
                item["recall_after_seconds"] = recall_seconds

            linked_commands = sanitize_linked_commands(payload.get("linked_commands"))
            if original_name and original_name != name and original_name in self.plugin.linked_tasks:
                self.plugin.linked_tasks[name] = self.plugin.linked_tasks.pop(original_name)
            if linked_commands:
                self.plugin.linked_tasks[name] = linked_commands
            else:
                self.plugin.linked_tasks.pop(name, None)

        item["created_at"] = item.get("created_at") or datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        if is_new:
            self.plugin.reminders.append(item)
            await self.plugin._schedule_reminder(item)
        else:
            await self.plugin._reschedule_reminder(item)

        save_reminders(self.plugin)
        return jsonify(
            {
                "ok": True,
                "item": serialize_item(
                    item,
                    None if is_task else self.plugin.linked_tasks.get(name, []),
                ),
                "limit_changed": limit_changed,
            }
        )

    async def delete_item(self):
        self._require_webui_context()
        payload = await request.get_json(force=True)
        is_task = bool(payload.get("is_task", False))
        name = validate_name(payload.get("name", ""))
        item = self._find_item(name, is_task)
        if item is None:
            raise ValueError("未找到目标对象")

        self.plugin._remove_all_jobs_for_item(item)
        self.plugin.reminders.remove(item)
        if not is_task:
            self.plugin.linked_tasks.pop(name, None)
        save_reminders(self.plugin)
        return jsonify({"ok": True})

    async def toggle_session(self):
        self._require_webui_context()
        payload = await request.get_json(force=True)
        is_task = bool(payload.get("is_task", False))
        name = validate_name(payload.get("name", ""))
        origin = str(payload.get("origin", "")).strip()
        enabled = bool(payload.get("enabled", False))

        item = self._find_item(name, is_task)
        if item is None:
            raise ValueError("未找到目标对象")
        if not origin:
            raise ValueError("缺少会话 origin")

        sessions = list(item.get("enabled_sessions", []))
        if enabled:
            if origin not in sessions:
                sessions.append(origin)
        else:
            sessions = [session for session in sessions if session != origin]
        if not sessions:
            raise ValueError("至少需要保留一个启用会话")

        item["enabled_sessions"] = sessions
        await self.plugin._reschedule_reminder(item)
        save_reminders(self.plugin)
        return jsonify({"ok": True})

    async def toggle_active(self):
        self._require_webui_context()
        payload = await request.get_json(force=True)
        is_task = bool(payload.get("is_task", False))
        name = validate_name(payload.get("name", ""))
        is_paused = bool(payload.get("is_paused", False))

        item = self._find_item(name, is_task)
        if item is None:
            raise ValueError("未找到目标对象")

        item["is_paused"] = is_paused
        await self.plugin._reschedule_reminder(item)
        save_reminders(self.plugin)
        return jsonify(
            {
                "ok": True,
                "item": serialize_item(
                    item,
                    None if is_task else self.plugin.linked_tasks.get(name, []),
                ),
            }
        )

    async def trigger_now(self):
        self._require_webui_context()
        payload = await request.get_json(force=True)
        is_task = bool(payload.get("is_task", False))
        name = validate_name(payload.get("name", ""))
        item = self._find_item(name, is_task)
        if item is None:
            raise ValueError("未找到目标对象")

        for session in item.get("enabled_sessions", []):
            if is_task:
                await execute_task(self.plugin, item, session)
            else:
                await send_reminder(self.plugin, item, session)
        return jsonify({"ok": True})

    async def upload_media(self, component_type: str):
        self._require_webui_context()
        files = await request.files
        file = files.get("file")
        if file is None:
            raise ValueError("未提供上传文件")

        component_type = str(component_type or "image").strip().lower()
        if component_type not in {"image", "record", "video"}:
            raise ValueError("不支持的媒体类型")

        suffix = Path(file.filename or "").suffix
        if not suffix:
            suffix = {
                "image": ".jpg",
                "record": ".ogg",
                "video": ".mp4",
            }[component_type]

        digest = hashlib.md5(
            f"{file.filename}-{datetime.datetime.now().timestamp()}".encode("utf-8")
        ).hexdigest()[:8]
        filename = f"{component_type}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{digest}{suffix}"
        target_path = Path(self.plugin.data_dir) / filename
        await file.save(target_path)

        return jsonify({"ok": True, "item": {"type": component_type, "path": filename}})

    def _find_item(self, name: str, is_task: bool) -> dict[str, Any] | None:
        for item in self.plugin.reminders:
            if bool(item.get("is_task", False)) == is_task and item.get("name") == name:
                return item
        return None

    def _find_any_item(self, name: str) -> dict[str, Any] | None:
        for item in self.plugin.reminders:
            if item.get("name") == name:
                return item
        return None

    def _ensure_name_available(
        self,
        name: str,
        is_task: bool,
        *,
        exclude_name: str | None = None,
    ) -> None:
        for item in self.plugin.reminders:
            if bool(item.get("is_task", False)) != is_task:
                continue
            if exclude_name and item.get("name") == exclude_name:
                continue
            if item.get("name") == name:
                kind = "任务" if is_task else "提醒"
                raise ValueError(f"已存在同名{kind}")

    def _build_new_item(self, name: str, is_task: bool) -> dict[str, Any]:
        return {
            "name": name,
            "is_task": is_task,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "created_by": "",
            "creator_name": "",
            "is_admin": False,
            "self_id": "",
        }

    def _require_webui_context(self) -> dict[str, Any]:
        payload = self.plugin.webui_context
        if not payload:
            raise ValueError("请先由 AstrBot admin 在聊天中使用“启动提醒控制台”后再通过 WebUI 编辑。")
        return payload

    def _apply_webui_context(self, item: dict[str, Any], payload: dict[str, Any]) -> None:
        item["created_by"] = str(payload.get("created_by", "") or "").strip()
        item["creator_name"] = str(payload.get("creator_name", "") or "").strip()
        item["is_admin"] = bool(payload.get("is_admin", False))
        item["self_id"] = str(payload.get("self_id", "") or "").strip()

    def _resolve_platform(self, item: dict[str, Any]) -> str:
        sessions = item.get("enabled_sessions", []) or []
        if sessions and ":" in sessions[0]:
            return sessions[0].split(":", 1)[0]

        for reminder in self.plugin.reminders:
            existing_sessions = reminder.get("enabled_sessions", []) or []
            if existing_sessions and ":" in existing_sessions[0]:
                return existing_sessions[0].split(":", 1)[0]
        return "aiocqhttp"

    def _validate_cron(self, cron_expr: str) -> None:
        if len(cron_expr.split()) != 5:
            raise ValueError("cron 需要 5 段")
        if CronTrigger is not None:
            CronTrigger.from_crontab(translate_to_apscheduler_cron(cron_expr))
