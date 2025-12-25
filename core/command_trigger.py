import asyncio
import contextvars
import time
from astrbot.api import logger
from astrbot.api.message_components import Plain, At
from astrbot.core.message.message_event_result import MessageChain

# 创建协程局部上下文标识，确保即使是同一个对象，不同的触发任务也互不干扰
_forwarding_lock = contextvars.ContextVar("forwarding_lock", default=False)

class CommandTrigger:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config or {}
        self.monitor_timeout = self.config.get('monitor_timeout', 60)
        self._monitor_task = None # 记录当前监控任务

    def _get_original_session_id(self, session_id: str) -> str:
        if "_" in session_id and ":" in session_id:
            parts = session_id.rsplit(":", 1)
            if len(parts) == 2 and "_" in parts[1]:
                group_id, _ = parts[1].rsplit("_", 1)
                return f"{parts[0]}:{group_id}"
        return session_id

    async def trigger_and_forward_command(self, unified_msg_origin: str, item: dict, command: str):
        self._monitor_task = asyncio.current_task() # 记录当前执行任务
        target_dest = self._get_original_session_id(unified_msg_origin)
        
        from .event_factory import EventFactory
        factory = EventFactory(self.context)
        event = factory.create_event(
            unified_msg_origin, command, item.get('created_by', 'timer'), 'Timer'
        )

        # 转发核心逻辑：带有 ContextVar 递归锁
        async def do_forward(chain, source):
            if _forwarding_lock.get(): return # 正在转发中，跳过，切断递归
            
            token = _forwarding_lock.set(True) # 上锁
            try:
                logger.info(f"[拦截成功] {source} -> 转发: {command}")
                await self.context.send_message(target_dest, chain)
            except Exception as e:
                logger.error(f"转发异常: {e}")
            finally:
                _forwarding_lock.reset(token) # 必须解锁

        # 劫持实例方法
        async def intercepted_send(message_chain):
            await do_forward(message_chain, "event.send")
            event._has_send_oper = True
            return True
        event.send = intercepted_send

        if hasattr(event, 'bot') and hasattr(event.bot, 'api'):
            original_call = event.bot.api.call_action
            async def intercepted_call(action, **params):
                send_actions = ["send_private_msg", "send_group_msg", "send_private_forward_msg", "send_group_forward_msg"]
                # 只有特定的发送动作且不在转发锁定时才拦截
                if not _forwarding_lock.get() and action in send_actions and "message" in params:
                    # 转换逻辑...
                    from astrbot.core.message.message_event_result import MessageChain as EventMessageChain
                    msg_chain = EventMessageChain()
                    message_data = params.get("message", [])
                    # 简化解析确保稳定
                    if isinstance(message_data, list):
                        for seg in message_data:
                            if isinstance(seg, dict) and seg.get("type") == "text":
                                msg_chain.chain.append(Plain(seg.get("data", {}).get("text", "")))
                    elif isinstance(message_data, str):
                        msg_chain.chain.append(Plain(message_data))

                    if msg_chain.chain:
                        await do_forward(msg_chain, f"call_action({action})")
                    return {"message_id": 12345}
                return await original_call(action, **params)
            event.bot.api.call_action = intercepted_call

        try:
            self.context.get_event_queue().put_nowait(event)
            await asyncio.sleep(self.monitor_timeout)
        except asyncio.CancelledError:
            logger.info(f"[强制取消] 指令监控任务被中止: {command}")
            raise # 向上抛出以允许 terminate 流程继续
        finally:
            self._monitor_task = None