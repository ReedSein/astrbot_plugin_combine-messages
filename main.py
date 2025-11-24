import asyncio
import uuid
import time
import json
from typing import Dict, List, Set, Any, Optional

from astrbot.api.all import * # 引入所有常用接口
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import (
    Plain, Image, At, File, Reply, Forward, Node, BaseMessageComponent
)
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import star_handlers_registry

class MessageBuffer:
    """
    消息缓冲池
    负责暂时持有碎片消息，并在超时后生成合并后的完整消息事件。
    """
    def __init__(self, context: Context):
        self.buffer_pool: Dict[str, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.context = context
        # 默认配置，会被 Plugin 类覆盖
        self.interval_time = 3.0
        self.initial_delay = 0.5

    def get_session_id(self, event: AstrMessageEvent) -> str:
        """生成唯一的会话ID，区分群聊和私聊"""
        if event.is_private_chat():
            return f"private_{event.get_sender_id()}"
        else:
            gid = getattr(event.message_obj, "group_id", "unknown")
            return f"group_{gid}_{event.get_sender_id()}"

    async def add_component(self, event: AstrMessageEvent, component: BaseMessageComponent) -> None:
        """向缓冲区添加一个组件"""
        sid = self.get_session_id(event)
        
        async with self.lock:
            # 1. 初始化会话缓冲
            if sid not in self.buffer_pool:
                self.buffer_pool[sid] = {
                    "components": [],       # 存储真实的组件对象
                    "timer": None,          # 合并倒计时任务
                    "event": event,         # 保留一份原始事件作为模板
                    "delay_task": None,     # 初始静默期任务
                }

            # 2. 智能文本合并 (避免 ["你", "好"] 这种情况)
            current_comps = self.buffer_pool[sid]["components"]
            if (isinstance(component, Plain) and 
                current_comps and 
                isinstance(current_comps[-1], Plain)):
                # 如果前后都是文本，合并到前一个 Plain 组件中
                # 添加一个空格或逗号可能更好，但在中文语境下直接拼接通常更自然
                # 这里选择直接拼接，并在中间加个空格防止英文粘连，中文通常不影响
                prev_text = current_comps[-1].text
                new_text = component.text
                sep = " " if prev_text and prev_text[-1].isascii() and new_text and new_text[0].isascii() else ""
                current_comps[-1].text += f"{sep}{new_text}"
            else:
                # 其他情况直接追加组件 (Image, At, File 等)
                current_comps.append(component)

            # 3. 重置计时器
            self._reset_timers(sid)
            
            # 更新最新事件引用 (确保元数据最新)
            self.buffer_pool[sid]["event"] = event
            
            # 启动初始延迟 (Debounce)
            self.buffer_pool[sid]["delay_task"] = asyncio.create_task(
                self._wait_and_start_merge(sid)
            )

    def _reset_timers(self, sid: str):
        """清理旧的计时器"""
        if sid not in self.buffer_pool: return
        
        for key in ["timer", "delay_task"]:
            task = self.buffer_pool[sid].get(key)
            if task:
                task.cancel()
                self.buffer_pool[sid][key] = None

    async def _wait_and_start_merge(self, sid: str) -> None:
        """第一阶段：初始静默期 (防止极其快速的连发被打断)"""
        try:
            await asyncio.sleep(self.initial_delay)
            async with self.lock:
                if sid in self.buffer_pool:
                    # 启动真正的合并倒计时
                    self.buffer_pool[sid]["timer"] = asyncio.create_task(
                        self._wait_and_merge(sid)
                    )
        except asyncio.CancelledError:
            pass

    async def _wait_and_merge(self, sid: str) -> None:
        """第二阶段：合并倒计时结束，执行合并"""
        try:
            await asyncio.sleep(self.interval_time)
            
            async with self.lock:
                buf = self.buffer_pool.get(sid)
                if not buf: return
                
                components = buf.get("components", [])
                base_event = buf.get("event")
                
                # 清理缓冲区
                self.buffer_pool.pop(sid, None)

                if not base_event or not components: return

                # --- 核心构建逻辑 ---
                
                # 1. 构建 message_str (仅用于 LLM 理解和日志显示)
                # 我们使用标准占位符，而不是试图解析 URL，保护隐私并简化逻辑
                str_parts = []
                for comp in components:
                    if isinstance(comp, Plain):
                        str_parts.append(comp.text.strip())
                    elif isinstance(comp, Image):
                        # [重要] 这里只生成文本占位符，不影响组件列表里的真实 Image 对象
                        str_parts.append("[图片]") 
                    elif isinstance(comp, At):
                        str_parts.append(f"@{comp.qq}")
                    elif isinstance(comp, File):
                        str_parts.append(f"[文件:{getattr(comp, 'name', '未知')}]")
                    else:
                        str_parts.append(f"[{type(comp).__name__}]")
                
                merged_str = " ".join(str_parts)
                if not merged_str.strip() and not components: return

                logger.info(f"[CombineMsg] 合并 {len(components)} 个片段 -> {merged_str[:50]}...")

                try:
                    # 2. 构建新的 AstrBotMessage 对象
                    new_message_obj = AstrBotMessage()
                    
                    # 复制基础属性
                    orig_msg = base_event.message_obj
                    new_message_obj.type = orig_msg.type
                    new_message_obj.self_id = orig_msg.self_id
                    new_message_obj.session_id = orig_msg.session_id
                    new_message_obj.group_id = getattr(orig_msg, "group_id", "")
                    new_message_obj.sender = orig_msg.sender
                    new_message_obj.raw_message = orig_msg.raw_message # 这里的 raw 可能不准确，但通常不影响
                    new_message_obj.timestamp = int(time.time())
                    
                    # 生成合成消息ID
                    original_id = getattr(orig_msg, "message_id", str(uuid.uuid4()))
                    new_message_obj.message_id = f"combined-{original_id}-{int(time.time()*1000)}"
                    
                    # [关键] 赋值核心数据
                    new_message_obj.message_str = merged_str
                    new_message_obj.message = components  # 包含原生 Image/File 对象的列表！

                    # 3. 构建新的 Event
                    event_args = {
                        "message_str": merged_str,
                        "message_obj": new_message_obj,
                        "platform_meta": base_event.platform_meta,
                        "session_id": base_event.session_id,
                    }
                    if hasattr(base_event, "bot"):
                        event_args["bot"] = base_event.bot

                    # 反射创建同类型的 Event (例如 AiocqhttpMessageEvent)
                    new_event = type(base_event)(**event_args)
                    new_event.is_wake = True # 标记为唤醒消息，防止被忽略

                    # 4. 重新注入处理队列
                    if self.context:
                        self.context.get_event_queue().put_nowait(new_event)
                    else:
                        logger.error("[CombineMsg] Context 丢失，无法推送消息")

                except Exception as e:
                    logger.error(f"[CombineMsg] 构建合并事件失败: {e}", exc_info=True)

        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        """关闭时清理所有挂起的任务"""
        async with self.lock:
            for sid, buf in list(self.buffer_pool.items()):
                self._reset_timers(sid)
            self.buffer_pool.clear()


@register("combine_messages", "合并消息", "自动合并连续消息，支持图文混排，完美兼容 SpectreCore", "3.0.0")
class CombineMessagesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.enabled = True
        self.interval_time = float(config.get("interval_time", 3.0))
        self.initial_delay = float(config.get("initial_delay", 0.5))
        
        self.message_buffer = MessageBuffer(context)
        self.message_buffer.interval_time = self.interval_time
        self.message_buffer.initial_delay = self.initial_delay

    async def initialize(self):
        logger.info(f"[CombineMsg] 插件已启动 (间隔: {self.interval_time}s, 延迟: {self.initial_delay}s)")

    async def shutdown(self):
        await self.message_buffer.shutdown()

    def _get_all_command_names(self) -> Set[str]:
        """动态获取系统内注册的所有指令名，防止合并指令"""
        if not hasattr(self, "_cmd_cache"):
            self._cmd_cache = set()
            self._cmd_cache_time = 0
        
        now = time.time()
        if now - self._cmd_cache_time < 60: # 缓存 60秒
            return self._cmd_cache

        cmds = set()
        # 1. 扫描所有注册的 handler
        for handler in star_handlers_registry:
            for f in getattr(handler, "event_filters", []):
                if isinstance(f, CommandFilter):
                    cmds.add(f.command_name)
                elif isinstance(f, CommandGroupFilter):
                    cmds.add(f.group_name)
        
        # 2. 添加配置中的额外指令
        extra = self.config.get("extra_commands", ["llm", "help", "start"])
        cmds.update(extra)
        
        self._cmd_cache = cmds
        self._cmd_cache_time = now
        return cmds

    # ================= 指令控制区域 =================

    @filter.command("combine_on")
    async def enable_combine(self, event: AstrMessageEvent):
        self.enabled = True
        yield event.plain_result("✅ 消息合并已开启")

    @filter.command("combine_off")
    async def disable_combine(self, event: AstrMessageEvent):
        self.enabled = False
        yield event.plain_result("❌ 消息合并已关闭")

    @filter.command("combine_interval")
    async def set_interval(self, event: AstrMessageEvent, seconds: str):
        try:
            val = float(seconds)
            val = max(0.5, min(val, 10.0)) # 限制在 0.5 ~ 10 秒之间
            self.interval_time = val
            self.message_buffer.interval_time = val
            self.config["interval_time"] = val
            self.config.save_config()
            yield event.plain_result(f"⏱️ 合并间隔已设置为 {val} 秒")
        except ValueError:
            yield event.plain_result("⚠️ 请输入有效的数字")

    # ================= 核心处理逻辑 =================

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=10 # [关键] 提高优先级，确保在 SpectreCore 等业务插件之前拦截
    )
    async def on_message(self, event: AstrMessageEvent):
        # 1. 基础检查
        if not self.enabled: return
        
        # 防止递归：如果消息已经是我们生成的（combined-开头），直接放行
        msg_id = getattr(event.message_obj, "message_id", "")
        if isinstance(msg_id, str) and msg_id.startswith("combined-"):
            return

        # 2. [SpectreCore 兼容性补丁] 
        # 检查是否包含复杂组件 (Reply, Forward)，如果包含，直接放行，不参与合并
        # 这些组件通常意味着强上下文关联，合并会破坏逻辑
        raw_chain = getattr(event.message_obj, "message", [])
        for comp in raw_chain:
            # 使用类型名称判断，兼容不同版本的导入
            ctype = comp.__class__.__name__
            if isinstance(comp, (Reply, Forward, Node)) or ctype in ["Reply", "Forward", "Node", "Json"]:
                logger.debug(f"[CombineMsg] 检测到特殊组件 {ctype}，跳过合并")
                return

        # 3. 指令检查
        msg_text = event.message_str.strip()
        
        # 获取阻塞前缀 (例如 / . #)
        block_prefixes = tuple(self.config.get("block_prefixes", ["/", "!", "！", ".", "。", "#", "%"]))
        
        # 检查是否是指令
        if msg_text.startswith(block_prefixes) or "[SYS_PROMPT]" in msg_text:
            return
            
        # 检查首个单词是否匹配已注册指令
        first_token = msg_text.split()[0] if msg_text else ""
        if first_token in self._get_all_command_names():
            return

        # 4. 入库逻辑
        has_content = False
        
        # 遍历消息链，只提取我们需要合并的类型 (Plain, Image, At, File)
        # 忽略未知的复杂类型，防止报错
        for comp in raw_chain:
            should_merge = False
            
            if isinstance(comp, Plain) and comp.text and comp.text.strip():
                # 二次检查 Plain 是否包含指令前缀（防止图文混排中的文字是指令）
                if comp.text.strip().startswith(block_prefixes): continue
                should_merge = True
                
            elif isinstance(comp, (Image, At, File)):
                should_merge = True
            
            if should_merge:
                await self.message_buffer.add_component(event, comp)
                has_content = True

        # 5. 如果成功添加了内容，拦截原事件
        if has_content:
            logger.debug(f"[CombineMsg] 拦截消息: {msg_text[:20]}...")
            event.stop_event()
