import asyncio
import contextvars
import time
import traceback
import os
from astrbot.api import logger
from astrbot.api.message_components import Plain, At, Image, Video, Record, Face
from astrbot.core.message.message_event_result import MessageChain

# 协程局部锁，防止转发时产生无限递归
_forwarding_lock = contextvars.ContextVar("forwarding_lock", default=False)

class CommandTrigger:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config or {}
        self.monitor_timeout = self.config.get('monitor_timeout', 60)
        self.captured_messages = []

    def _get_original_session_id(self, session_id: str) -> str:
        if "_" in session_id and ":" in session_id:
            parts = session_id.rsplit(":", 1)
            if len(parts) == 2 and "_" in parts[1]:
                group_id, _ = parts[1].rsplit("_", 1)
                return f"{parts[0]}:{group_id}"
        return session_id

    def _clean_path(self, path_str: str) -> str:
        """清洗路径，去掉 file:// 等协议头，防止路径冲突"""
        if not path_str: return path_str
        if path_str.startswith("file:///"):
            return path_str[8:]
        if path_str.startswith("file:/"):
            return path_str[6:]
        return path_str

    async def trigger_and_forward_command(self, unified_msg_origin: str, item: dict, command: str):
        target_dest = self._get_original_session_id(unified_msg_origin)
        
        from .event_factory import EventFactory
        factory = EventFactory(self.context)
        event = factory.create_event(
            unified_msg_origin, command, item.get('created_by', 'timer'), 'Timer'
        )

        # --- 核心转发函数 ---
        async def do_forward(chain, source_info):
            if _forwarding_lock.get(): return 
            
            token = _forwarding_lock.set(True)
            try:
                # 检查 chain 是否为有效的 MessageChain 对象
                if chain and hasattr(chain, 'chain') and chain.chain:
                    logger.info(f"[拦截成功] {source_info} -> 正在转发: {command}")
                    self.captured_messages.append(chain)
                    # 关键：确保调用成功
                    await self.context.send_message(target_dest, chain)
            except Exception:
                logger.error(f"[转发失败] {source_info} 发生异常:\n{traceback.format_exc()}")
            finally:
                _forwarding_lock.reset(token)

        # 1. 劫持 event.send
        original_send = event.send
        async def intercepted_send(message_chain):
            if _forwarding_lock.get(): return await original_send(message_chain)
            await do_forward(message_chain, "event.send")
            event._has_send_oper = True
            return True
        event.send = intercepted_send

        # 2. 劫持 bot.api.call_action
        original_call = None
        if hasattr(event, 'bot') and hasattr(event.bot, 'api'):
            original_call = event.bot.api.call_action
            
            async def intercepted_call(action, **params):
                send_actions = ["send_private_msg", "send_group_msg", "send_private_forward_msg", "send_group_forward_msg"]
                
                target_id = target_dest.split(':')[-1]
                current_dest = str(params.get('group_id') or params.get('user_id') or "")
                
                if _forwarding_lock.get() or action not in send_actions or current_dest != target_id:
                    return await original_call(action, **params)

                # 解析消息
                msg_chain = MessageChain()
                raw_msg = params.get("message")
                
                if isinstance(raw_msg, dict):
                    msg_elements = [raw_msg]
                elif isinstance(raw_msg, list):
                    msg_elements = raw_msg
                else:
                    msg_elements = [{"type": "text", "data": {"text": str(raw_msg)}}]

                for seg in msg_elements:
                    if not isinstance(seg, dict): continue
                    stype = seg.get("type")
                    data = seg.get("data", {})
                    
                    try:
                        if stype == "text":
                            msg_chain.chain.append(Plain(data.get("text", "")))
                        elif stype in ["image", "video", "record"]:
                            # 获取路径或URL
                            file_val = data.get("file") or data.get("url")
                            if not file_val: continue
                            
                            file_str = str(file_val)
                            is_url = file_str.startswith("http")
                            clean_val = self._clean_path(file_str)
                            
                            if stype == "image":
                                msg_chain.chain.append(Image.fromURL(clean_val) if is_url else Image.fromFileSystem(clean_val))
                            elif stype == "video":
                                msg_chain.chain.append(Video.fromURL(clean_val) if is_url else Video.fromFileSystem(clean_val))
                            elif stype == "record":
                                msg_chain.chain.append(Record.fromURL(clean_val) if is_url else Record.fromFileSystem(clean_val))
                        elif stype == "at":
                            msg_chain.chain.append(At(qq=data.get("qq")))
                        elif stype == "face":
                            msg_chain.chain.append(Face(id=data.get("id")))
                    except Exception as conv_err:
                        logger.error(f"组件转换异常({stype}): {conv_err}")

                if msg_chain.chain:
                    await do_forward(msg_chain, f"API:{action}")
                
                event._has_send_oper = True
                return {"status": "ok", "retcode": 0, "data": {"message_id": int(time.time())}, "message_id": int(time.time())}

            event.bot.api.call_action = intercepted_call

        try:
            self.context.get_event_queue().put_nowait(event)
            await asyncio.sleep(self.monitor_timeout)
        except asyncio.CancelledError:
            raise
        finally:
            # 恢复环境
            event.send = original_send
            if original_call and hasattr(event, 'bot') and hasattr(event.bot, 'api'):
                event.bot.api.call_action = original_call
            logger.info(f"[任务结束] {command} 监控退出")