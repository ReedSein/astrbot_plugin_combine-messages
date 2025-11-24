import asyncio
import uuid
import time
import json
from typing import Dict, Set, Any

from astrbot.api.all import * from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import (
    Plain, Image, At, File, Reply, Forward, Node, BaseMessageComponent
)
from astrbot.api.platform import AstrBotMessage

class MessageBuffer:
    """
    强一致性消息缓冲池
    逻辑：收到第一条消息 -> 启动固定倒计时 -> 倒计时结束 -> 强制发送
    """
    def __init__(self, context: Context):
        self.buffer_pool: Dict[str, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.context = context
        # 默认配置
        self.max_wait_time = 2.0  # 最大等待时间（秒）

    def get_session_id(self, event: AstrMessageEvent) -> str:
        if event.is_private_chat():
            return f"private_{event.get_sender_id()}"
        else:
            gid = getattr(event.message_obj, "group_id", "unknown")
            return f"group_{gid}_{event.get_sender_id()}"

    async def add_component(self, event: AstrMessageEvent, component: BaseMessageComponent) -> None:
        sid = self.get_session_id(event)
        
        async with self.lock:
            # 1. 如果是该会话的第一条消息，初始化缓冲区并启动倒计时
            if sid not in self.buffer_pool:
                self.buffer_pool[sid] = {
                    "components": [],       
                    "event": event,         
                    "timer": asyncio.create_task(self._countdown_and_send(sid)) # 启动发车倒计时
                }
                logger.debug(f"[CombineMsg] 会话 {sid} 启动合并窗口，等待 {self.max_wait_time}s")

            # 2. 文本合并逻辑 (优化体验)
            current_comps = self.buffer_pool[sid]["components"]
            if (isinstance(component, Plain) and 
                current_comps and 
                isinstance(current_comps[-1], Plain)):
                # 简单拼接，中间加空格
                current_comps[-1].text += " " + component.text
            else:
                current_comps.append(component)

            # 更新最新事件引用
            self.buffer_pool[sid]["event"] = event

    async def _countdown_and_send(self, sid: str) -> None:
        """核心发车逻辑：睡够时间，然后发送"""
        try:
            # 硬等待，不接受任何打断（除非 shutdown）
            await asyncio.sleep(self.max_wait_time)
            
            async with self.lock:
                buf = self.buffer_pool.get(sid)
                if not buf: return
                
                components = buf.get("components", [])
                base_event = buf.get("event")
                
                # 彻底移除缓冲区，准备下一次
                self.buffer_pool.pop(sid, None)

                if not base_event or not components: return
                
                # 开始构建发送
                await self._dispatch_merged_event(base_event, components)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[CombineMsg] 倒计时异常: {e}")

    async def _dispatch_merged_event(self, base_event: AstrMessageEvent, components: list):
        """构建并分发合并后的事件"""
        try:
            # 构建显示用的 message_str
            str_parts = []
            for comp in components:
                if isinstance(comp, Plain): str_parts.append(comp.text.strip())
                elif isinstance(comp, Image): str_parts.append("[图片]") 
                elif isinstance(comp, At): str_parts.append(f"@{comp.qq}")
                elif isinstance(comp, File): str_parts.append(f"[文件:{getattr(comp, 'name', '未知')}]")
                else: str_parts.append(f"[{type(comp).__name__}]")
            
            merged_str = " ".join(str_parts)
            if not merged_str.strip() and not components: return

            logger.info(f"[CombineMsg] 🚀 发车! 合并内容: {merged_str[:100]}")

            # 构建新对象
            new_message_obj = AstrBotMessage()
            orig_msg = base_event.message_obj
            
            # 复制属性
            for attr in ['type', 'self_id', 'session_id', 'group_id', 'sender', 'raw_message']:
                if hasattr(orig_msg, attr):
                    setattr(new_message_obj, attr, getattr(orig_msg, attr))
            
            new_message_obj.timestamp = int(time.time())
            original_id = getattr(orig_msg, "message_id", str(uuid.uuid4()))
            new_message_obj.message_id = f"combined-{original_id}-{int(time.time()*1000)}"
            
            # 注入合并后的数据
            new_message_obj.message_str = merged_str
            new_message_obj.message = components 

            # 构建事件
            event_args = {
                "message_str": merged_str,
                "message_obj": new_message_obj,
                "platform_meta": base_event.platform_meta,
                "session_id": base_event.session_id,
            }
            if hasattr(base_event, "bot"): event_args["bot"] = base_event.bot

            new_event = type(base_event)(**event_args)
            new_event.is_wake = True 

            # 推送
            if self.context:
                self.context.get_event_queue().put_nowait(new_event)
            else:
                logger.error("[CombineMsg] Context 丢失")

        except Exception as e:
            logger.error(f"[CombineMsg] 构建合并事件失败: {e}", exc_info=True)

    async def shutdown(self) -> None:
        async with self.lock:
            for sid, buf in list(self.buffer_pool.items()):
                if buf.get("timer"): buf["timer"].cancel()
            self.buffer_pool.clear()


@register("combine_messages", "合并消息", "强一致性合并消息插件", "3.1.0-Fixed")
class CombineMessagesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.enabled = True
        # 这里的 wait_time 就是“绝对等待时间”
        self.wait_time = float(config.get("wait_time", 2.0))
        
        self.message_buffer = MessageBuffer(context)
        self.message_buffer.max_wait_time = self.wait_time

    async def initialize(self):
        logger.info(f"[CombineMsg] 插件已启动 (绝对等待窗口: {self.wait_time}s)")

    async def shutdown(self):
        await self.message_buffer.shutdown()

    def _get_all_command_names(self) -> Set[str]:
        if not hasattr(self, "_cmd_cache"):
            self._cmd_cache = set()
            self._cmd_cache_time = 0
        now = time.time()
        if now - self._cmd_cache_time < 60: return self._cmd_cache
        cmds = set()
        for handler in star_handlers_registry:
            for f in getattr(handler, "event_filters", []):
                if isinstance(f, CommandFilter): cmds.add(f.command_name)
                elif isinstance(f, CommandGroupFilter): cmds.add(f.group_name)
        extra = self.config.get("extra_commands", ["llm", "help", "start", "reset"])
        cmds.update(extra)
        self._cmd_cache = cmds
        self._cmd_cache_time = now
        return cmds

    # ================= 指令 =================

    @filter.command("combine_on")
    async def enable_combine(self, event: AstrMessageEvent):
        self.enabled = True
        yield event.plain_result("✅ 消息合并已开启")

    @filter.command("combine_off")
    async def disable_combine(self, event: AstrMessageEvent):
        self.enabled = False
        yield event.plain_result("❌ 消息合并已关闭")

    @filter.command("combine_time")
    async def set_time(self, event: AstrMessageEvent, seconds: str):
        """设置绝对等待时间"""
        try:
            val = float(seconds)
            val = max(0.5, min(val, 10.0))
            self.wait_time = val
            self.message_buffer.max_wait_time = val
            self.config["wait_time"] = val
            self.config.save_config()
            yield event.plain_result(f"⏱️ 绝对等待时间已设置为 {val} 秒")
        except ValueError:
            yield event.plain_result("⚠️ 请输入有效的数字")

    # ================= 监听 =================

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=10
    )
    async def on_message(self, event: AstrMessageEvent):
        if not self.enabled: return
        
        # 1. 防止死循环
        msg_id = getattr(event.message_obj, "message_id", "")
        if isinstance(msg_id, str) and msg_id.startswith("combined-"): return

        # 2. [SpectreCore 兼容] 放行特殊组件
        raw_chain = getattr(event.message_obj, "message", [])
        for comp in raw_chain:
            ctype = comp.__class__.__name__
            if isinstance(comp, (Reply, Forward, Node)) or ctype in ["Reply", "Forward", "Node"]:
                logger.debug(f"[CombineMsg] 放行特殊组件: {ctype}")
                return

        # 3. 指令检查
        msg_text = event.message_str.strip()
        block_prefixes = tuple(self.config.get("block_prefixes", ["/", "!", "！", ".", "。", "#", "%"]))
        if msg_text.startswith(block_prefixes) or "[SYS_PROMPT]" in msg_text: return
        first_token = msg_text.split()[0] if msg_text else ""
        if first_token in self._get_all_command_names(): return

        # 4. 拦截并入库
        has_content = False
        for comp in raw_chain:
            should_merge = False
            if isinstance(comp, Plain) and comp.text and comp.text.strip():
                if comp.text.strip().startswith(block_prefixes): continue
                should_merge = True
            elif isinstance(comp, (Image, At, File)):
                should_merge = True
            
            if should_merge:
                await self.message_buffer.add_component(event, comp)
                has_content = True

        if has_content:
            # logger.debug(f"[CombineMsg] 拦截: {msg_text[:10]}...") 
            event.stop_event()
