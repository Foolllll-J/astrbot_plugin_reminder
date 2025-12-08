from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import json
import os
from typing import Dict, List
import aiohttp
import shutil

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
            unified_msg_origin = reminder.get('unified_msg_origin')
            if not unified_msg_origin:
                logger.warning(f"无法发送提醒 '{reminder.get('name', 'unknown')}'，unified_msg_origin 未设置")
                return
            
            # 按照原始顺序构建消息
            chain = []
            for item in reminder['message_structure']:
                if item['type'] == 'text':
                    chain.append(Plain(item['content']))
                elif item['type'] == 'image':
                    full_path = os.path.join(self.data_dir, item['path'])
                    if os.path.exists(full_path):
                        chain.append(Image.fromFileSystem(full_path))
                    else:
                        logger.warning(f"图片文件不存在: {full_path}")
            
            if not chain:
                logger.warning(f"提醒消息为空: {reminder['name']}")
                return
            
            # 使用 MessageChain 发送消息
            message_chain = MessageChain()
            message_chain.chain = chain
            await self.context.send_message(unified_msg_origin, message_chain)
            
            logger.info(f"提醒已发送: {reminder['name']} -> {unified_msg_origin}")
            
        except Exception as e:
            logger.error(f"发送提醒失败: {reminder.get('name', 'unknown')}, {e}", exc_info=True)

    @filter.command("添加提醒")
    async def add_reminder(self, event: AstrMessageEvent):
        """添加定时提醒
        格式1（当前会话）: /添加提醒 <提醒名称> <cron表达式> <消息内容> [图片]
        格式2（指定群号）: /添加提醒 <提醒名称> @<群号> <cron表达式> <消息内容> [图片]
        示例: /添加提醒 每日提醒 0 9 * * * 早上好！[并附上图片]
        """
        
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        try:
            # 解析文本参数
            parts = event.message_str.strip().split(maxsplit=2)
            
            if len(parts) < 3:
                yield event.plain_result(
                    "格式错误！\n"
                    "用法1（当前会话）: /添加提醒 <提醒名称> <cron表达式(5段)> <消息内容>\n"
                    "用法2（指定群聊）: /添加提醒 <提醒名称> @<群号> <cron表达式(5段)> <消息内容>\n"
                    "cron表达式格式: 分 时 日 月 周\n"
                    "示例1: /添加提醒 早安 0 9 * * * 早上好！\n"
                    "示例2: /添加提醒 早安 @123456 0 9 * * * 早上好！\n"
                    "💡 可以在发送指令的同时附上图片，提醒时会一起发送文字和图片\n"
                    "💡 不指定群号时，会自动发送到当前会话"
                )
                return
            
            _, name, remaining = parts
            
            # 检查提醒名称是否重复
            for existing_reminder in self.reminders:
                if existing_reminder['name'] == name:
                    yield event.plain_result(f"❌ 提醒名称 '{name}' 已存在，请使用不同的名称")
                    return
            
            # 尝试解析是否包含目标群号（格式如 @123456）
            remaining_parts = remaining.split(maxsplit=1)
            if len(remaining_parts) >= 2 and remaining_parts[0].startswith('@'):
                # 格式2：指定了目标群号
                group_id = remaining_parts[0][1:]  # 去掉 @ 符号
                remaining = remaining_parts[1]
                
                # 构建 unified_msg_origin
                # 从当前会话中提取平台信息
                current_origin = event.unified_msg_origin
                if ':' in current_origin:
                    platform = current_origin.split(':')[0]
                    unified_msg_origin = f"{platform}:GroupMessage:{group_id}"
                    logger.info(f"检测到目标群号: {group_id}, 构建会话ID: {unified_msg_origin}")
                else:
                    yield event.plain_result("❌ 无法识别当前平台信息，请使用当前会话模式")
                    return
            else:
                # 格式1：使用当前会话
                unified_msg_origin = event.unified_msg_origin
                logger.info(f"使用当前会话ID: {unified_msg_origin}")
            
            # 解析cron表达式（需要5段）
            # 使用 maxsplit=5 来分割，前5段是cron表达式，剩余的都是消息内容
            remaining_parts = remaining.split(maxsplit=5)
            
            if len(remaining_parts) < 5:
                yield event.plain_result(
                    "cron表达式格式错误！需要5段: 分 时 日 月 周\n"
                    "示例: 0 9 * * * 表示每天9点0分"
                )
                return
            
            # 前5段是cron表达式，第6段（如果存在）是消息内容
            # 注意：如果消息前面有图片，第5段可能会和消息文本粘在一起（如 "*1072248491"）
            # 需要清理第5段，只保留合法的cron字符
            cron_parts = remaining_parts[:5]
            
            # 清理第5段（周），只保留合法的cron值
            # 周的合法格式：数字(0-6)、*、范围(如1-5)、列表(如1,3,5)、步长(如*/2)
            last_part = cron_parts[4]
            cleaned_last_part = ''
            
            # 策略：遇到空格或其他明显的文本内容就停止
            # 合法字符：0-9, *, -, ,, /
            # 但要防止过长的数字串（如 1072248491）
            for i, char in enumerate(last_part):
                if char in '0123456789*-,/':
                    # 检查是否是异常长的数字（超过2位连续数字很可能是文本内容）
                    if char.isdigit():
                        # 向后看，如果有超过2位连续数字，可能是文本
                        digit_count = 1
                        for j in range(i + 1, min(i + 10, len(last_part))):
                            if last_part[j].isdigit():
                                digit_count += 1
                            else:
                                break
                        # 周的数字范围是 0-6，最多2位（比如 */10 这种步长）
                        # 如果有超过3位连续数字，很可能是文本内容粘上来了
                        if digit_count > 3:
                            break
                    cleaned_last_part += char
                else:
                    # 遇到非法字符，停止
                    break
            
            if not cleaned_last_part:
                # 如果清理后为空，说明格式错误
                yield event.plain_result(
                    "cron表达式格式错误！第5段（周）无效\n"
                    "示例: 0 9 * * * 表示每天9点0分"
                )
                return
            
            cron_parts[4] = cleaned_last_part
            cron_expr = ' '.join(cron_parts)
            
            # 消息文本是第6段（如果有），加上从第5段截断的部分
            message_text = ""
            if len(remaining_parts) > 5:
                message_text = remaining_parts[5]
            # 加上从第5段截断的部分
            if len(last_part) > len(cleaned_last_part):
                message_text = last_part[len(cleaned_last_part):] + (' ' + message_text if message_text else '')
            
            message_text = message_text.strip()
            
            # 验证cron表达式
            try:
                CronTrigger.from_crontab(cron_expr)
            except Exception as e:
                logger.error(f"cron表达式验证失败: {e}")
                yield event.plain_result(f"cron表达式无效: {e}")
                return
            
            # 提取完整的消息结构（图文混排）
            # 策略：直接在消息链中查找 cron 表达式的结束位置
            message_structure = []
            message_chain = event.get_messages()
            
            # 遍历消息链，在 Plain 中找到 cron 表达式的结束位置
            cron_found = False
            
            for msg_comp in message_chain:
                if isinstance(msg_comp, Plain):
                    if not cron_found and cron_expr in msg_comp.text:
                        # 找到了 cron 表达式
                        cron_index = msg_comp.text.index(cron_expr)
                        cron_end = cron_index + len(cron_expr)
                        
                        # 提取 cron 之后的文本
                        content = msg_comp.text[cron_end:]
                        cron_found = True
                        
                        if content.strip():
                            message_structure.append({
                                "type": "text",
                                "content": content
                            })
                    elif cron_found:
                        # 已经找到 cron，后续文本直接添加
                        if msg_comp.text.strip():
                            message_structure.append({
                                "type": "text",
                                "content": msg_comp.text
                            })
                        
                elif isinstance(msg_comp, Image):
                    # 图片只在找到 cron 之后添加
                    if cron_found:
                        img_filename = f"img_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
                        img_path = os.path.join(self.data_dir, img_filename)
                        
                        try:
                            saved = False
                            if msg_comp.url:
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(msg_comp.url) as resp:
                                        if resp.status == 200:
                                            with open(img_path, 'wb') as f:
                                                f.write(await resp.read())
                                            saved = True
                            elif msg_comp.file:
                                shutil.copy(msg_comp.file, img_path)
                                saved = True
                            
                            if saved:
                                message_structure.append({
                                    "type": "image",
                                    "path": img_filename
                                })
                        except Exception as e:
                            logger.error(f"保存图片失败: {e}")
            
            # 验证至少有消息内容
            if not message_structure:
                yield event.plain_result("提醒内容不能为空，请至少提供文字或图片")
                return
            
            # 创建提醒对象
            reminder_id = f"reminder_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(self.reminders)}"
            reminder = {
                'id': reminder_id,
                'name': name,
                'unified_msg_origin': unified_msg_origin,
                'cron': cron_expr,
                'message_structure': message_structure,  # 保存完整的消息结构
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'created_by': event.get_sender_id()
            }
            
            # 添加到调度器
            self._add_job(reminder)
            
            # 保存到列表
            self.reminders.append(reminder)
            self._save_reminders()
            
            is_current_session = (unified_msg_origin == event.unified_msg_origin)
            if is_current_session:
                target_desc = "当前会话"
            else:
                # 提取群号显示
                if ':GroupMessage:' in unified_msg_origin:
                    group_id = unified_msg_origin.split(':GroupMessage:')[1]
                    target_desc = f"群聊 {group_id}"
                else:
                    target_desc = unified_msg_origin
            
            # 统计消息内容
            text_count = sum(1 for item in message_structure if item['type'] == 'text')
            image_count = sum(1 for item in message_structure if item['type'] == 'image')
            
            result_msg = f"✅ 提醒已添加！\n名称: {name}\n目标: {target_desc}\ncron: {cron_expr}"
            if text_count > 0:
                result_msg += f"\n文字: {text_count}段"
            if image_count > 0:
                result_msg += f"\n图片: {image_count}张"
            
            logger.info(f"成功添加提醒: {name}, unified_msg_origin: {unified_msg_origin}, cron: {cron_expr}")
            yield event.plain_result(result_msg)
            
        except Exception as e:
            logger.error(f"添加提醒失败: {e}", exc_info=True)
            yield event.plain_result(f"添加提醒失败: {e}")

    @filter.command("查看提醒")
    async def list_reminders(self, event: AstrMessageEvent, name: str = ""):
        """查看提醒任务
        用法1: /查看提醒 - 查看所有提醒任务列表
        用法2: /查看提醒 <提醒名称> - 查看指定提醒的详细信息（包含完整文字和图片）
        """
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        if not self.reminders:
            yield event.plain_result("当前没有提醒任务")
            return
        
        # 解析参数：检查是否指定了提醒名称
        params = name.strip()
        
        if params:
            # 查看指定提醒的详细信息
            reminder_name = params
            target_reminder = None
            
            # 查找匹配的提醒任务
            for reminder in self.reminders:
                if reminder['name'] == reminder_name:
                    target_reminder = reminder
                    break
            
            if not target_reminder:
                yield event.plain_result(f"❌ 未找到名为 '{reminder_name}' 的提醒任务\n\n💡 使用 /查看提醒 查看所有提醒任务列表")
                return
            
            # 构建消息链：先添加属性信息
            chain = []
            
            # 格式化目标显示
            target = target_reminder.get('unified_msg_origin', '未知')
            info_text = f"📋 提醒详情: {target_reminder['name']}\n\n"
            
            if ':GroupMessage:' in target:
                group_id = target.split(':GroupMessage:')[1]
                info_text += f"🎯 发送目标: 群聊 {group_id}\n"
            elif ':FriendMessage:' in target:
                friend_id = target.split(':FriendMessage:')[1]
                info_text += f"🎯 发送目标: 私聊 {friend_id}\n"
            else:
                info_text += f"🎯 发送目标: {target}\n"
            
            info_text += f"⏰ 定时规则: {target_reminder['cron']}\n"
            info_text += f"📅 创建时间: {target_reminder['created_at']}\n"
            info_text += f"👤 创建者ID: {target_reminder.get('created_by', '未知')}\n"
            info_text += f"\n📝 提醒内容:\n"
            
            chain.append(Plain(info_text))
            
            # 按照原始顺序构建提醒内容
            for item in target_reminder['message_structure']:
                if item['type'] == 'text':
                    chain.append(Plain(item['content']))
                elif item['type'] == 'image':
                    full_path = os.path.join(self.data_dir, item['path'])
                    if os.path.exists(full_path):
                        chain.append(Image.fromFileSystem(full_path))
                    else:
                        logger.warning(f"图片文件不存在: {full_path}")
            
            # 使用 MessageChain 返回
            message_chain = MessageChain()
            message_chain.chain = chain
            yield event.chain_result(message_chain.chain)
        
        else:
            # 显示所有提醒任务列表（简略信息）
            result = "📋 当前提醒列表:\n\n"
            for idx, reminder in enumerate(self.reminders, 1):
                result += f"{idx}. {reminder['name']}\n"
                
                # 格式化目标显示
                target = reminder.get('unified_msg_origin', '未知')
                if ':GroupMessage:' in target:
                    group_id = target.split(':GroupMessage:')[1]
                    result += f"   目标: 群聊 {group_id}\n"
                elif ':FriendMessage:' in target:
                    friend_id = target.split(':FriendMessage:')[1]
                    result += f"   目标: 私聊 {friend_id}\n"
                else:
                    result += f"   目标: {target}\n"
                
                result += f"   cron: {reminder['cron']}\n"
                
                # 统计内容
                text_count = sum(1 for item in reminder['message_structure'] if item['type'] == 'text')
                image_count = sum(1 for item in reminder['message_structure'] if item['type'] == 'image')
                
                content_parts = []
                if text_count > 0:
                    content_parts.append(f"文字{text_count}段")
                if image_count > 0:
                    content_parts.append(f"图片{image_count}张")
                
                if content_parts:
                    result += f"   内容: {' + '.join(content_parts)}\n"
                
                result += f"   创建时间: {reminder['created_at']}\n\n"
            
            result += "💡 使用 /查看提醒 <提醒名称> 查看详细内容"
            
            yield event.plain_result(result)

    @filter.command("删除提醒")
    async def delete_reminder(self, event: AstrMessageEvent, index: int):
        """删除提醒任务
        用法: /删除提醒 <序号>
        """
        # 权限检查：仅管理员可用
        if not event.is_admin():
            yield event.plain_result("❌ 此指令仅限Bot管理员使用")
            return
        
        try:
            # 先检查是否有提醒任务
            if len(self.reminders) == 0:
                yield event.plain_result("❌ 当前没有提醒任务")
                return
            
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
            for item in reminder['message_structure']:
                if item['type'] == 'image':
                    img_path = os.path.join(self.data_dir, item['path'])
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
用法1（当前会话）: /添加提醒 <名称> <cron表达式> <消息>
用法2（指定群聊）: /添加提醒 <名称> @<群号> <cron表达式> <消息>
- cron表达式: 5段格式 (分 时 日 月 周)
- 💡 不指定群号时，自动发送到当前会话
- 💡 指定群号时，只能指定群聊，不支持私聊
- 💡 发送指令时可以同时附上图片，提醒会包含文字+图片
- 🔒 仅限Bot管理员使用

基础示例:
/添加提醒 早安 0 9 * * * 早上好！
(每天9点在当前会话发送)

/添加提醒 周报 @123456789 0 18 * * 5 本周工作总结
(每周五18点在指定群聊发送，可实现远程控制)

⭐ 多时间点示例（用逗号分隔）:
/添加提醒 喝水 0 9,14,18 * * * 记得喝水！
(每天9点、14点、18点各发送一次)

/添加提醒 课间休息 0 10,15,20 * * 1-5 该休息了
(工作日10点、15点、20点发送)

/添加提醒 整点报时 0 */2 * * * 当前时间...
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
/查看提醒 - 查看所有提醒任务列表
/查看提醒 <提醒名称> - 查看指定提醒的详细内容（包含完整文字和图片）

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
