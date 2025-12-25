import asyncio
from astrbot.api import logger
from astrbot.api.message_components import Plain, At
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain
from .event_factory import EventFactory


class CommandTrigger:
    """指令触发器，用于触发其他插件指令并转发结果"""

    def __init__(self, context, wechat_platforms, config=None):
        self.context = context
        self.wechat_platforms = wechat_platforms
        self.config = config or {}
        self.captured_messages = []  # 存储捕获到的消息
        self.original_send_method = None  # 保存原始的send方法
        self.target_event = None  # 目标事件对象
        self.event_factory = EventFactory(context)  # 事件工厂

    def _add_at_message(self, msg_chain, original_msg_origin, reminder):
        """添加@消息的helper函数"""
        platform_type = self._get_platform_type_from_origin(original_msg_origin)
        if platform_type == "aiocqhttp":
            # QQ平台 - 优先使用昵称，回退到ID
            if "created_by" in reminder and reminder["created_by"]:
                msg_chain.chain.append(At(qq=reminder["created_by"], name=reminder.get("created_by_name", "用户")))
            else:
                msg_chain.chain.append(At(qq=reminder["created_by"]))
        elif platform_type in self.wechat_platforms:
            if "created_by_name" in reminder and reminder["created_by_name"]:
                msg_chain.chain.append(Plain(f"@{reminder['created_by_name']} "))
            else:
                msg_chain.chain.append(Plain(f"@{reminder['created_by']} "))
        else:
            msg_chain.chain.append(Plain(f"@{reminder['created_by']} "))

    def _get_platform_type_from_origin(self, unified_msg_origin: str) -> str:
        """从unified_msg_origin中获取平台类型"""
        if ":" in unified_msg_origin:
            platform_part = unified_msg_origin.split(":")[0]
            # 根据平台名称映射到实际平台类型
            platform_mapping = {
                "aiocqhttp": "aiocqhttp",
                "qq_official": "qq_official",
                "telegram": "telegram",
                "discord": "discord",
                "slack": "slack",
                "lark": "lark",
                "wechatpadpro": "wechatpadpro",
                "webchat": "webchat",
                "dingtalk": "dingtalk",
            }
            return platform_mapping.get(platform_part, platform_part)
        return "unknown"

    def _is_private_chat(self, unified_msg_origin: str) -> bool:
        """判断是否为私聊"""
        return ":FriendMessage:" in unified_msg_origin

    def _is_group_chat(self, unified_msg_origin: str) -> bool:
        """判断是否为群聊"""
        return (":GroupMessage:" in unified_msg_origin) or ("@chatroom" in unified_msg_origin)

    def _get_original_session_id(self, session_id: str) -> str:
        """从隔离格式的会话ID中提取原始会话ID，用于消息发送"""
        # 处理其他平台的情况
        if "_" in session_id and ":" in session_id:
            # 非微信平台，使用通用规则
            parts = session_id.rsplit(":", 1)
            if len(parts) == 2 and "_" in parts[1]:
                # 查找最后一个下划线，认为这是会话隔离添加的
                group_id, user_id = parts[1].rsplit("_", 1)
                return f"{parts[0]}:{group_id}"

        # 如果不是隔离格式或无法解析，返回原始ID
        return session_id

    def setup_message_interceptor(self, target_event):
        """设置消息拦截器来捕获指令的响应"""
        self.target_event = target_event
        self.captured_messages = []

        # 保存原始的send方法
        if self.original_send_method is None:
            self.original_send_method = target_event.send

        # 保存原始的bot.api.call_action方法（如果存在）
        self.original_call_action = None
        if hasattr(target_event, 'bot') and hasattr(target_event.bot, 'api') and hasattr(target_event.bot.api, 'call_action'):
            self.original_call_action = target_event.bot.api.call_action

        # 创建拦截器包装函数
        async def intercepted_send(message_chain):
            # 捕获这条消息
            logger.info(f"捕获到指令响应消息，包含 {len(message_chain.chain)} 个组件")
            self.captured_messages.append(message_chain)

            # 设置已发送标记，但不实际发送到平台
            target_event._has_send_oper = True
            return True

        # 创建bot.api.call_action拦截器
        async def intercepted_call_action(action, **params):
            # 检查是否是发送消息的action
            if action in ["send_private_msg", "send_group_msg", "send_private_forward_msg", "send_group_forward_msg"]:
                logger.info(f"拦截到bot.api.call_action调用: {action}")

                # 从params中提取消息内容并转换为MessageChain
                if "message" in params:
                    from astrbot.core.message.message_event_result import MessageChain
                    from astrbot.api.message_components import Plain, At

                    msg_chain = MessageChain()
                    message_data = params["message"]

                    # 处理消息数据
                    if isinstance(message_data, list):
                        for seg in message_data:
                            if isinstance(seg, dict):
                                seg_type = seg.get("type", "")
                                seg_data = seg.get("data", {})

                                if seg_type == "text":
                                    msg_chain.chain.append(Plain(seg_data.get("text", "")))
                                elif seg_type == "at":
                                    qq = seg_data.get("qq", "")
                                    msg_chain.chain.append(At(qq=qq))
                                # 可以根据需要添加更多消息类型的处理

                    # 捕获转换后的消息
                    if msg_chain.chain:
                        logger.info(f"从bot.api.call_action捕获到消息，包含 {len(msg_chain.chain)} 个组件")
                        self.captured_messages.append(msg_chain)

                # 设置已发送标记，但不实际发送
                target_event._has_send_oper = True
                return {"message_id": 12345}  # 返回一个假的message_id
            else:
                # 对于非发送消息的action，调用原始方法
                if self.original_call_action:
                    return await self.original_call_action(action, **params)
                else:
                    return {}

        # 替换事件的send方法
        target_event.send = intercepted_send

        # 替换bot.api.call_action方法（如果存在）
        if self.original_call_action:
            target_event.bot.api.call_action = intercepted_call_action

        logger.info(f"已设置消息拦截器，监听事件: {target_event.unified_msg_origin}")

    def restore_message_sender(self):
        """恢复原始的消息发送器"""
        if self.original_send_method and self.target_event:
            self.target_event.send = self.original_send_method
            logger.info("已恢复原始消息发送器")

        # 恢复原始的bot.api.call_action方法
        if hasattr(self, 'original_call_action') and self.original_call_action and self.target_event:
            if hasattr(self.target_event, 'bot') and hasattr(self.target_event.bot, 'api'):
                self.target_event.bot.api.call_action = self.original_call_action
                logger.info("已恢复原始bot.api.call_action方法")

    def create_command_event(self, unified_msg_origin: str, command: str, creator_id: str, creator_name: str = None) -> AstrMessageEvent:
        """创建指令事件对象"""
        return self.event_factory.create_event(unified_msg_origin, command, creator_id, creator_name)

    async def trigger_and_capture_command(self, unified_msg_origin: str, command: str, creator_id: str, creator_name: str = None):
        """触发指令并捕获响应"""
        try:
            logger.info(f"开始触发指令: {command}")

            # 创建指令事件
            fake_event = self.create_command_event(unified_msg_origin, command, creator_id, creator_name)

            # 设置消息拦截器
            self.setup_message_interceptor(fake_event)

            # 提交事件到事件队列
            event_queue = self.context.get_event_queue()
            event_queue.put_nowait(fake_event)

            logger.info(f"已将指令事件 {command} 提交到事件队列")

            # 等待指令执行并捕获响应
            max_wait_time = self.config.get('monitor_timeout', 60.0)  # 使用配置的监控超时时间
            wait_interval = 0.1   # 每100毫秒检查一次
            waited_time = 0.0

            logger.info(f"开始监控消息捕获，最多等待 {max_wait_time} 秒，检查间隔 {wait_interval} 秒")

            while waited_time < max_wait_time:
                await asyncio.sleep(wait_interval)
                waited_time += wait_interval

                # 在配置的时间内持续监控，不管距离上次响应的时间
                current_message_count = len(self.captured_messages)
                if current_message_count > 0:
                    logger.info(f"已捕获到 {current_message_count} 条响应消息，继续监控直到超时...")

            # 恢复原始消息发送器
            self.restore_message_sender()

            if self.captured_messages:
                return True, self.captured_messages
            else:
                logger.warning(f"等待 {max_wait_time} 秒后未捕获到指令 {command} 的响应消息")
                return False, []

        except Exception as e:
            logger.error(f"触发指令失败: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())

            # 确保恢复原始消息发送器
            self.restore_message_sender()
            return False, []

    async def trigger_and_forward_command(self, unified_msg_origin: str, reminder: dict, command: str):
        """触发指令并转发结果（用于定时任务）"""
        creator_id = reminder.get("created_by", "unknown")
        creator_name = reminder.get("created_by_name")

        # 获取实际执行的命令
        actual_command = command

        # 触发指令并捕获响应
        success, captured_messages = await self.trigger_and_capture_command(
            unified_msg_origin, actual_command, creator_id, creator_name
        )

        if success and captured_messages:
            logger.info(f"成功捕获到指令 {command} 的 {len(captured_messages)} 条响应，开始转发")

            # 转发捕获到的消息
            for i, captured_msg in enumerate(captured_messages):
                logger.info(f"转发第 {i+1} 条消息，包含 {len(captured_msg.chain)} 个组件")

                # 获取原始消息ID用于发送
                original_msg_origin = self._get_original_session_id(unified_msg_origin)

                # 构建消息
                forward_msg = MessageChain()

                # 添加@消息（如果需要）
                should_at = True  # 默认需要@，可以根据配置调整
                if should_at and not self._is_private_chat(unified_msg_origin) and "created_by" in reminder and reminder["created_by"]:
                    self._add_at_message(forward_msg, original_msg_origin, reminder)

                # 添加捕获到的消息内容
                for component in captured_msg.chain:
                    forward_msg.chain.append(component)

                # 发送转发消息
                await self.context.send_message(original_msg_origin, forward_msg)

                # 如果有多条消息，添加间隔
                if len(captured_messages) > 1 and i < len(captured_messages) - 1:
                    await asyncio.sleep(0.5)
        else:
            logger.warning(f"未能捕获到指令 {command} 的响应，发送错误提示")

            # 发送执行失败的提示
            original_msg_origin = self._get_original_session_id(unified_msg_origin)

            error_msg = MessageChain()

            # 添加@消息（如果需要）
            should_at = True
            if should_at and not self._is_private_chat(unified_msg_origin) and "created_by" in reminder and reminder["created_by"]:
                self._add_at_message(error_msg, original_msg_origin, reminder)

            error_msg.chain.append(Plain(f"[指令任务] {command} 执行失败，未收到响应"))

            await self.context.send_message(original_msg_origin, error_msg)