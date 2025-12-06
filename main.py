from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import json
import os
from typing import Dict, List, Optional

@register("astrbot_plugin_reminder", "Foolllll", "定时提醒插件，支持cron表达式和图片消息", "0.1.0")
class ReminderPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.scheduler = AsyncIOScheduler()
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_reminder")
        os.makedirs(self.data_dir, exist_ok=True)
        self.data_file = os.path.join(self.data_dir, "reminders.json")
        self.reminders: List[Dict] = []
        self.bot = None
        self._load_reminders()
        logger.info("定时提醒插件已加载")

    async def initialize(self):
        """初始化插件，启动调度器"""
        self._restore_reminders()
        self.scheduler.start()
        logger.info(f"定时提醒插件启动成功，已加载 {len(self.reminders)} 个提醒任务")

    def _load_reminders(self):
        """从文件加载提醒数据"""
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    self.reminders = json.load(f)
            except Exception as e:
                logger.error(f"加载提醒数据失败: {e}")
                self.reminders = []
        else:
            self.reminders = []

    def _save_reminders(self):
        """保存提醒数据到文件"""
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.reminders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存提醒数据失败: {e}")

    def _restore_reminders(self):
        """恢复所有提醒任务到调度器"""
        for reminder in self.reminders:
            try:
                self._add_job(reminder)
            except Exception as e:
                logger.error(f"恢复提醒任务失败: {e}, 任务: {reminder}")

    def _add_job(self, reminder: Dict):
        """添加任务到调度器"""
        job_id = reminder['id']
        cron_expr = reminder['cron']
        
        # 解析cron表达式 (分 时 日 月 周)
        parts = cron_expr.split()
        if len(parts) != 5:
            raise ValueError(f"无效的cron表达式: {cron_expr}")
        
        minute, hour, day, month, day_of_week = parts
        
        self.scheduler.add_job(
            self._send_reminder,
            CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week
            ),
            args=[reminder],
            id=job_id,
            replace_existing=True
        )

    async def _send_reminder(self, reminder: Dict):
        """发送提醒消息"""
        try:
            if not self.bot:
                logger.warning(f"[发送提醒] 无法发送提醒，因为尚未捕获到 bot 实例。请先触发任意一次指令。")
                return
            
            target_type = reminder['target_type']
            target_id = reminder['target_id']
            message_content = reminder['message']
            
            logger.info(f"[发送提醒] 开始发送提醒: {reminder['name']}, 目标: {target_type}:{target_id}")
            
            # 构建消息内容
            message_parts = []
            
            # 处理文本消息
            if message_content.get('text'):
                message_parts.append({"type": "text", "data": {"text": message_content['text']}})
                logger.info(f"[发送提醒] 添加文本: {message_content['text']}")
            
            # 处理图片消息
            if message_content.get('images'):
                for img_path in message_content['images']:
                    full_path = os.path.join(self.data_dir, img_path)
                    if os.path.exists(full_path):
                        message_parts.append({"type": "image", "data": {"file": f"file:///{os.path.abspath(full_path)}"}})
                        logger.info(f"[发送提醒] 添加图片: {img_path}")
                    else:
                        logger.warning(f"[发送提醒] 图片文件不存在: {full_path}")
            
            if not message_parts:
                logger.warning(f"[发送提醒] 提醒消息为空，跳过发送: {reminder}")
                return
            
            # 发送消息
            if target_type == 'group':
                send_result = await self.bot.api.call_action('send_group_msg', group_id=int(target_id), message=message_parts)
            else:
                send_result = await self.bot.api.call_action('send_private_msg', user_id=int(target_id), message=message_parts)
            
            logger.info(f"[发送提醒] 发送成功: {send_result}, 提醒: {reminder['name']} 到 {target_type}:{target_id}")
            
        except Exception as e:
            logger.error(f"[发送提醒] 发送失败: {e}, 任务: {reminder}", exc_info=True)

    @filter.command("添加提醒")
    async def add_reminder(self, event: AstrMessageEvent):
        """添加定时提醒
        格式: /添加提醒 <提醒名称> <目标类型> <目标ID> <cron表达式> <消息内容> [图片]
        示例: /添加提醒 每日提醒 群组 123456 0 9 * * * 早上好！[并附上图片]
        """
        # 捕获 bot 实例
        if not self.bot:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent):
                self.bot = event.bot
        
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        try:
            # 先提取消息中的图片
            images = []
            message_chain = event.get_messages()
            for msg_comp in message_chain:
                if isinstance(msg_comp, Image):
                    # 保存图片到data目录
                    img_filename = f"img_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(images)}.jpg"
                    img_path = os.path.join(self.data_dir, img_filename)
                    
                    try:
                        # 下载并保存图片
                        if msg_comp.url:
                            import aiohttp
                            async with aiohttp.ClientSession() as session:
                                async with session.get(msg_comp.url) as resp:
                                    if resp.status == 200:
                                        with open(img_path, 'wb') as f:
                                            f.write(await resp.read())
                                        images.append(img_filename)
                                        logger.info(f"已保存图片: {img_filename}")
                        elif msg_comp.file:
                            # 如果是本地文件，直接复制
                            import shutil
                            shutil.copy(msg_comp.file, img_path)
                            images.append(img_filename)
                            logger.info(f"已复制图片: {img_filename}")
                    except Exception as e:
                        logger.error(f"保存图片失败: {e}")
            
            # 解析文本参数
            parts = event.message_str.strip().split(maxsplit=5)
            logger.info(f"[添加提醒] 收到指令，用户: {event.get_sender_name()}({event.get_sender_id()})")
            logger.info(f"[添加提醒] 原始消息: {event.message_str}")
            logger.info(f"[添加提醒] 分割后参数数量: {len(parts)}, 参数: {parts}")
            
            if len(parts) < 6:
                logger.warning(f"[添加提醒] 参数不足，需要6个参数，实际: {len(parts)}")
                yield event.plain_result(
                    "格式错误！\n"
                    "用法: /添加提醒 <提醒名称> <目标类型> <目标ID> <cron表达式(5段)> <消息内容>\n"
                    "目标类型: 群组 或 私聊\n"
                    "cron表达式格式: 分 时 日 月 周\n"
                    "示例: /添加提醒 早安 群组 123456 0 9 * * * 早上好！\n"
                    "💡 可以在发送指令的同时附上图片，提醒时会一起发送文字和图片"
                )
                return
            
            _, name, target_type_str, target_id, cron_expr_part, message_text = parts
            logger.info(f"[添加提醒] 解析参数 - 名称: {name}, 目标类型: {target_type_str}, 目标ID: {target_id}")
            logger.info(f"[添加提醒] cron部分: {cron_expr_part}, 消息: {message_text}")
            
            # 验证目标类型
            if target_type_str not in ['群组', '私聊']:
                logger.warning(f"[添加提醒] 目标类型错误: {target_type_str}")
                yield event.plain_result("目标类型必须是 '群组' 或 '私聊'")
                return
            
            target_type = 'group' if target_type_str == '群组' else 'private'
            
            # 解析cron表达式（需要5段）
            cron_parts = cron_expr_part.split()
            message_parts = message_text.split()
            
            logger.info(f"[添加提醒] cron初始分割: {cron_parts}, 剩余消息: {message_parts}")
            
            # cron需要5段，所以从message_text中取出剩余部分
            while len(cron_parts) < 5 and message_parts:
                cron_parts.append(message_parts.pop(0))
            
            logger.info(f"[添加提醒] cron最终: {cron_parts}, 最终消息: {message_parts}")
            
            if len(cron_parts) != 5:
                logger.warning(f"[添加提醒] cron表达式段数错误: {len(cron_parts)}")
                yield event.plain_result(
                    "cron表达式格式错误！需要5段: 分 时 日 月 周\n"
                    "示例: 0 9 * * * 表示每天9点0分"
                )
                return
            
            cron_expr = ' '.join(cron_parts)
            message_text = ' '.join(message_parts) if message_parts else ""
            
            logger.info(f"[添加提醒] 最终cron: {cron_expr}, 最终消息文本: {message_text}")
            
            # 验证cron表达式
            try:
                CronTrigger.from_crontab(cron_expr)
                logger.info(f"[添加提醒] cron表达式验证通过: {cron_expr}")
            except Exception as e:
                logger.error(f"[添加提醒] cron表达式验证失败: {e}")
                yield event.plain_result(f"cron表达式无效: {e}")
                return
            
            # 验证至少有文字或图片
            if not message_text and not images:
                logger.warning(f"[添加提醒] 提醒内容为空")
                yield event.plain_result("提醒内容不能为空，请至少提供文字或图片")
                return
            
            logger.info(f"[添加提醒] 提醒内容 - 文字: '{message_text}', 图片数: {len(images)}")
            
            # 创建提醒对象
            reminder_id = f"reminder_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(self.reminders)}"
            reminder = {
                'id': reminder_id,
                'name': name,
                'target_type': target_type,
                'target_id': target_id,
                'cron': cron_expr,
                'message': {
                    'text': message_text,
                    'images': images
                },
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'created_by': event.get_sender_id()
            }
            
            logger.info(f"[添加提醒] 创建提醒对象: {reminder_id}")
            
            # 添加到调度器
            self._add_job(reminder)
            logger.info(f"[添加提醒] 已添加到调度器")
            
            # 保存到列表
            self.reminders.append(reminder)
            self._save_reminders()
            logger.info(f"[添加提醒] 已保存到文件，当前提醒总数: {len(self.reminders)}")
            
            result_msg = f"✅ 提醒已添加！\n名称: {name}\n目标: {target_type_str} {target_id}\ncron: {cron_expr}"
            if message_text:
                result_msg += f"\n文字: {message_text}"
            if images:
                result_msg += f"\n图片: {len(images)}张"
            
            logger.info(f"[添加提醒] 成功添加提醒: {name}")
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"[添加提醒] 失败: {e}", exc_info=True)
            yield event.plain_result(f"添加提醒失败: {e}")

    @filter.command("查看提醒")
    async def list_reminders(self, event: AstrMessageEvent):
        """查看所有提醒任务"""
        # 捕获 bot 实例
        if not self.bot:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent):
                self.bot = event.bot
        
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        if not self.reminders:
            yield event.plain_result("当前没有提醒任务")
            return
        
        result = "📋 当前提醒列表:\n\n"
        for idx, reminder in enumerate(self.reminders, 1):
            target_type_str = "群组" if reminder['target_type'] == 'group' else "私聊"
            result += f"{idx}. {reminder['name']}\n"
            result += f"   目标: {target_type_str} {reminder['target_id']}\n"
            result += f"   cron: {reminder['cron']}\n"
            msg_text = reminder['message']['text']
            if msg_text:
                preview = msg_text[:30] + "..." if len(msg_text) > 30 else msg_text
                result += f"   文字: {preview}\n"
            if reminder['message'].get('images'):
                result += f"   图片: {len(reminder['message']['images'])}张\n"
            result += f"   创建时间: {reminder['created_at']}\n\n"
        
        yield event.plain_result(result)

    @filter.command("删除提醒")
    async def delete_reminder(self, event: AstrMessageEvent, index: int):
        """删除提醒任务
        用法: /删除提醒 <序号>
        """
        # 捕获 bot 实例
        if not self.bot:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            if isinstance(event, AiocqhttpMessageEvent):
                self.bot = event.bot
        
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        try:
            if index < 1 or index > len(self.reminders):
                yield event.plain_result(f"序号无效，请输入1-{len(self.reminders)}之间的数字")
                return
            
            reminder = self.reminders[index - 1]
            
            # 从调度器移除
            try:
                self.scheduler.remove_job(reminder['id'])
            except Exception as e:
                logger.warning(f"从调度器移除任务失败: {e}")
            
            # 删除关联的图片文件
            if reminder['message'].get('images'):
                for img_filename in reminder['message']['images']:
                    img_path = os.path.join(self.data_dir, img_filename)
                    try:
                        if os.path.exists(img_path):
                            os.remove(img_path)
                    except Exception as e:
                        logger.error(f"删除图片文件失败: {e}")
            
            # 从列表移除
            self.reminders.pop(index - 1)
            self._save_reminders()
            
            yield event.plain_result(f"✅ 已删除提醒: {reminder['name']}")
            
        except Exception as e:
            logger.error(f"删除提醒失败: {e}")
            yield event.plain_result(f"删除提醒失败: {e}")

    @filter.command("提醒帮助")
    async def show_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """📖 定时提醒插件使用帮助

🔹 添加提醒
/添加提醒 <名称> <目标类型> <目标ID> <cron表达式> <消息>
- 目标类型: 群组 或 私聊
- cron表达式: 5段格式 (分 时 日 月 周)
- 💡 发送指令时可以同时附上图片，提醒会包含文字+图片
- 🔒 仅限Bot管理员使用

基础示例:
/添加提醒 早安 群组 123456 0 9 * * * 早上好！
(每天9点发送)

/添加提醒 周报 私聊 987654 0 18 * * 5 本周工作总结
(每周五18点发送)

⭐ 多时间点示例（用逗号分隔）:
/添加提醒 喝水 群组 123456 0 9,14,18 * * * 记得喝水！
(每天9点、14点、18点各发送一次)

/添加提醒 课间休息 群组 123456 0 10,15,20 * * 1-5 该休息了
(工作日10点、15点、20点发送)

/添加提醒 整点报时 群组 123456 0 */2 * * * 当前时间...
(每2小时发送)

🔹 cron表达式详解
格式: 分 时 日 月 周
- 分: 0-59
- 时: 0-23
- 日: 1-31
- 月: 1-12
- 周: 0-6 (0=周日)

特殊符号:
- *: 任意值
- */n: 每n个单位
- a,b,c: 多个具体值（逗号分隔）
- a-b: 范围值

常用示例:
0 9 * * * - 每天9点
0 */2 * * * - 每2小时
30 18 * * 1-5 - 工作日18:30
0 0 1 * * - 每月1号0点
0 9,12,18 * * * - 每天9点、12点、18点
30 8-17/2 * * * - 8:30到17:30之间，每2小时

🔹 查看提醒
/查看提醒

🔹 删除提醒
/删除提醒 <序号>

🔹 帮助
/提醒帮助
"""
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件卸载时关闭调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()
        logger.info("定时提醒插件已卸载")
