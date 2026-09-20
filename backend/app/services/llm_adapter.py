"""LLM 协议适配层 — 借鉴 DSH 桌面端 adapter.ts 的多协议设计。

DSH 的核心设计：
  - LlmAdapter 是抽象基类，每个具体 adapter（DeepSeekAdapter、PiAiAdapter）
    负责一种 API 协议的序列化、请求发送、SSE 解析和响应翻译
  - adapter 只管传输，不管业务逻辑（上下文管理、token 计算等）
  - serialize.ts 负责将 harness 的中性消息格式序列化为 provider 特有的 wire 格式
  - translate.ts 负责将 provider 返回的 SSE chunk 翻译为 harness 的 StreamChunk

本模块用同样的模式实现两种协议：
  1. OpenAIAdapter — 调用 /chat/completions，OpenAI 兼容格式
  2. AnthropicAdapter — 调用 /v1/messages，Anthropic Messages API 格式

选择哪种协议由 AIProvider.api_type 字段决定。
"""

import json
import logging
import re
from typing import Optional, Callable

import httpx

logger = logging.getLogger(__name__)


class LlmAdapter:
    """LLM 协议适配器基类 — 对应 DSH 的 LlmAdapter。

    职责：
    - 构建请求 URL
    - 构建请求 headers
    - 序列化 messages 为 provider wire 格式
    - 发送流式请求
    - 解析 SSE 响应
    """

    def build_url(self, base_url: str) -> str:
        raise NotImplementedError

    def build_headers(self, api_key: str) -> dict:
        raise NotImplementedError

    def serialize_payload(
        self,
        model: str,
        messages: list[dict],
        persona: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list[dict]] = None,
    ) -> dict:
        """将中性消息格式序列化为 provider wire 格式。

        Args:
            tools: OpenAI function calling 格式的工具 schema 列表
        """
        raise NotImplementedError

    def parse_stream_line(self, line: str) -> Optional[tuple[str, str]]:
        """解析 SSE 行，返回 (text_delta, finish_reason) 或 None。

        返回值：
        - (text, None) — 文本增量
        - (text, "stop"|"length"|"tool_calls"|...) — 文本增量 + finish_reason
        - (None, None) — 空行或非数据行
        - usage 信息通过回调返回
        """
        raise NotImplementedError

    def extract_usage(self, chunk: dict) -> Optional[dict]:
        """从 SSE chunk 中提取 usage 信息。"""
        raise NotImplementedError

    def extract_reasoning(self, chunk: dict) -> Optional[str]:
        """从流式 chunk 中提取推理内容增量（reasoning_content / reasoning）。

        支持 DeepSeek 系（delta.reasoning_content）与 OpenAI 系（delta.reasoning）。
        不支持推理输出的模型返回 None（调用方静默忽略）。
        """
        return None

    def parse_tool_calls_delta(self, chunk: dict) -> Optional[list[dict]]:
        """从流式 chunk 中解析 tool_calls 增量。

        返回 tool_calls 列表（可能不完整，需调用方累积），或 None。
        每个 dict 格式：
        - {"index": int, "id": str|None, "function": {"name": str|None, "arguments": str|None}}
        """
        raise NotImplementedError


class OpenAIAdapter(LlmAdapter):
    """OpenAI 兼容协议适配器 — 对应 DSH 的 DeepSeekAdapter。

    请求端点: {base_url}/chat/completions
    请求格式: OpenAI Chat Completions API
    SSE 格式: data: {"choices":[{"delta":{"content":"..."}}]}
    终止标记: data: [DONE]

    借鉴 DSH serialize.ts 的设计：
    - stream: true
    - stream_options: { include_usage: true }（让流式最后一帧带回 usage，供任务级
      token 成本落库；个别端点不认此参数会 400，llm_service 检测到后摘除该参数
      降级重试一次，不再重复携带）
    - system 消息作为 messages[0]
    """

    def build_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/chat/completions"

    def build_headers(self, api_key: str) -> dict:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def serialize_payload(
        self,
        model: str,
        messages: list[dict],
        persona: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list[dict]] = None,
        thinking: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> dict:
        # ── 借鉴 DSH serialize.ts 的 requestWithMessages ──
        # OpenAI 格式：system 消息在 messages[0]
        wire_messages = []
        if persona:
            wire_messages.append({"role": "system", "content": persona})
        wire_messages.extend(messages)

        payload = {
            "model": model,
            "messages": wire_messages,
            "stream": True,
            # 让最后一帧带回 usage（任务级 token 成本落库）；
            # 端点不支持时由 llm_service 摘除参数降级重试
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        # ── 推理等级 — 借鉴 DSH serialize.ts 的 resolveThinking() ──
        # DSH wire 格式（types.ts WireRequest）:
        #   thinking: { type: 'enabled' | 'disabled' }  ← 注意是对象不是字符串
        #   reasoning_effort: 'low' | 'high' | 'max'    ← 不含 'off'
        # resolveThinking() 逻辑:
        #   effort='off'    → thinking: {type: 'disabled'}, 不发 reasoning_effort
        #   effort='low'    → thinking: {type: 'enabled'},  reasoning_effort: 'low'
        #   effort='high'   → thinking: {type: 'enabled'},  reasoning_effort: 'high'
        #   effort='max'    → thinking: {type: 'enabled'},  reasoning_effort: 'max'
        #   effort=None     → 不发任何推理参数（让 API 用默认行为）
        if reasoning_effort is not None:
            if reasoning_effort == "off":
                payload["thinking"] = {"type": "disabled"}
            elif reasoning_effort in ("minimal", "low", "medium", "high", "xhigh", "max"):
                payload["thinking"] = {"type": "enabled"}
                # 只发送 API 认识的值（minimal/medium/xhigh 不是 DeepSeek 的标准值，
                # 但 OpenAI 兼容 API 可能支持，直接传递让 API 决定）
                payload["reasoning_effort"] = reasoning_effort

        # ── 工具定义（function calling）──
        if tools:
            payload["tools"] = tools
        return payload

    def parse_stream_line(self, line: str) -> Optional[tuple[str, str]]:
        # 兼容 "data: " 和 "data:" 前缀
        if not line:
            return None
        if line.startswith("data: "):
            data = line[6:]
        elif line.startswith("data:"):
            data = line[5:]
        else:
            return None

        if data == "[DONE]":
            return ("", "__done__")

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return None

        choices = chunk.get("choices", [])
        text = ""
        finish_reason = None
        if choices:
            delta = choices[0].get("delta", {})
            text = delta.get("content", "") or ""
            finish_reason = choices[0].get("finish_reason")
            # tool_calls 的 finish_reason
            if finish_reason == "tool_calls":
                pass  # 由调用方通过 has_tool_calls 判断

        return (text, finish_reason)

    def extract_usage(self, chunk: dict) -> Optional[dict]:
        usage = chunk.get("usage")
        if not usage:
            return None
        # 性能优化7：捕获 provider 端 prompt cache 命中 token
        # OpenAI 系：usage.prompt_tokens_details.cached_tokens
        # DeepSeek：usage.prompt_cache_hit_tokens（命中部分仍计入 prompt_tokens，
        # 但按折扣计费——命中量是缓存生效的直接证据）
        cache_read = None
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
            cache_read = details["cached_tokens"]
        if cache_read is None and isinstance(usage.get("prompt_cache_hit_tokens"), int):
            cache_read = usage["prompt_cache_hit_tokens"]
        return {
            "total_tokens": usage.get("total_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cache_read_tokens": cache_read,
        }

    def parse_tool_calls_delta(self, chunk: dict) -> Optional[list[dict]]:
        """从 OpenAI 流式 chunk 中解析 tool_calls 增量。

        OpenAI 流式格式中，tool_calls 以增量方式到达：
        第一个 chunk: delta.tool_calls[0] = {index:0, id:"call_xxx", function:{name:"read_file", arguments:""}}
        后续 chunks: delta.tool_calls[0] = {index:0, function:{arguments:"{\"path\":\""}}
        最后: choices[0].finish_reason = "tool_calls"
        """
        choices = chunk.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        tool_calls = delta.get("tool_calls")
        if not tool_calls:
            return None
        return tool_calls

    def extract_reasoning(self, chunk: dict) -> Optional[str]:
        """提取推理内容增量：DeepSeek 系 delta.reasoning_content，OpenAI 系 delta.reasoning。"""
        choices = chunk.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        text = delta.get("reasoning_content")
        if text is None:
            text = delta.get("reasoning")
        return text if isinstance(text, str) and text else None


class AnthropicAdapter(LlmAdapter):
    """Anthropic Messages API 适配器。

    请求端点: {base_url}/v1/messages
    请求格式: Anthropic Messages API
    SSE 格式: event: content_block_delta / data: {"type":"content_block_delta","delta":{"text":"..."}}
    终止标记: event: message_stop

    借鉴 DSH PiAiAdapter 的设计：
    - system 消息不在 messages 数组中，而是单独的 system 参数
    - 认证使用 x-api-key header 而非 Bearer token
    - 需要 anthropic-version header

    Anthropic SSE 事件类型：
    - message_start: 消息开始，包含 usage (input_tokens)
    - content_block_start: 内容块开始
    - content_block_delta: 文本增量
    - content_block_stop: 内容块结束
    - message_delta: 消息级别增量，包含 usage (output_tokens)
    - message_stop: 消息结束
    """

    # Anthropic API 版本
    ANTHROPIC_VERSION = "2023-06-01"

    def build_url(self, base_url: str) -> str:
        # base_url 可能是 "https://api.xiaomimimo.com/anthropic"
        # 端点为 {base_url}/v1/messages
        return f"{base_url.rstrip('/')}/v1/messages"

    def build_headers(self, api_key: str) -> dict:
        return {
            "x-api-key": api_key,
            "content-type": "application/json",
            "anthropic-version": self.ANTHROPIC_VERSION,
        }

    # ── OpenAI wire 格式 → Anthropic wire 格式的消息转换 ──
    # Agent 循环（llm_service.chat_stream）内部使用 OpenAI 消息格式：
    #   - 工具调用：assistant 消息带 tool_calls 数组
    #   - 工具结果：{"role": "tool", "tool_call_id": "...", "content": "..."}
    #   - 多模态图片：user content 数组里的 {"type": "image_url", "image_url": {"url": ...}}
    # Anthropic Messages API 的对应格式完全不同：
    #   - 工具调用：assistant content 里的 {"type": "tool_use", "id", "name", "input"} block
    #   - 工具结果：user content 里的 {"type": "tool_result", "tool_use_id", "content"} block
    #   - 图片：{"type": "image", "source": {"type": "base64", "media_type", "data"}} block
    # 且角色必须严格 user/assistant 交替（连续同角色会被 API 拒绝）、
    # 每个 tool_use 的 tool_result 必须出现在紧随其后的那条消息里，
    # 因此连续的 tool 消息要合并进同一条 user 消息。

    @staticmethod
    def _merge_wire_blocks(wire_messages: list, role: str, blocks: list) -> None:
        """把 Anthropic content blocks 并入 wire 消息。

        与上一条 wire 消息同角色则合并（保持角色交替），否则新建。
        wire 消息全部新建 dict，绝不引用调用方传入的消息对象——
        合并会原地修改 content，复用外部 dict 会污染调用方的 agent_messages。
        """
        if wire_messages and wire_messages[-1].get("role") == role:
            prev = wire_messages[-1]
            if isinstance(prev["content"], str):
                prev["content"] = [{"type": "text", "text": prev["content"]}] if prev["content"] else []
            prev["content"].extend(blocks)
        else:
            wire_messages.append({"role": role, "content": blocks})

    @staticmethod
    def _assistant_tool_blocks(msg: dict) -> list:
        """OpenAI assistant tool_calls 消息 → Anthropic tool_use content blocks。"""
        blocks = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "input": args if isinstance(args, dict) else {},
            })
        return blocks

    @staticmethod
    def _convert_user_blocks(parts: list) -> list:
        """OpenAI 多模态 content（text + image_url）→ Anthropic content blocks。

        data URL（前端附件与工具返回图片的实际形态）转 base64 source；
        http(s) URL 转 url source。
        """
        blocks = []
        for part in parts:
            ptype = part.get("type")
            if ptype == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif ptype == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if not url:
                    continue
                if url.startswith("data:") and "," in url:
                    header, _, data = url.partition(",")
                    media_type = header[5:].split(";", 1)[0] or "image/png"
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    })
                else:
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                blocks.append(part)
        return blocks

    def serialize_payload(
        self,
        model: str,
        messages: list[dict],
        persona: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[list[dict]] = None,
        thinking: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> dict:
        # ── Anthropic 格式 ──
        # system 不在 messages 中，而是顶层参数
        # messages 只包含 user/assistant 消息（OpenAI 格式的 tool 消息与
        # tool_calls/image_url 结构在转换后才能被 Anthropic API 接受）
        # Anthropic 要求 max_tokens，默认值 4096
        wire_messages = []
        system_text = persona or ""
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                # 从 messages 中提取 system 消息
                system_text = (system_text + "\n\n" + msg["content"]).strip() if system_text else msg["content"]
            elif role == "tool":
                # OpenAI 工具结果 → user 消息里的 tool_result block
                self._merge_wire_blocks(wire_messages, "user", [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id") or "",
                    "content": msg.get("content") or "",
                }])
            elif role == "assistant" and msg.get("tool_calls"):
                # OpenAI tool_calls → assistant 消息里的 tool_use blocks
                self._merge_wire_blocks(wire_messages, "assistant", self._assistant_tool_blocks(msg))
            elif role == "user" and isinstance(msg.get("content"), list):
                # OpenAI 多模态 content（text + image_url）→ Anthropic blocks
                self._merge_wire_blocks(wire_messages, "user", self._convert_user_blocks(msg["content"]))
            else:
                # 纯文本消息原样透传（复制 dict，防止后续合并修改到调用方数据）
                wire_messages.append({"role": role or "user", "content": msg.get("content") or ""})

        payload = {
            "model": model,
            "messages": wire_messages,
            "max_tokens": max_tokens or 4096,
            "stream": True,
        }
        if system_text:
            payload["system"] = system_text
        if temperature is not None:
            payload["temperature"] = temperature
        # ── 工具定义（Anthropic 格式转换）──
        if tools:
            # Anthropic 使用不同的 tools 格式
            anthropic_tools = []
            for t in tools:
                fn = t.get("function", {})
                anthropic_tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
            payload["tools"] = anthropic_tools
        return payload

    def parse_stream_line(self, line: str) -> Optional[tuple[str, str]]:
        """解析 Anthropic SSE 行。

        Anthropic SSE 使用 event: 和 data: 双行格式。
        本解析器只看 data: 行，通过 JSON 中的 type 字段判断事件类型。
        """
        if not line:
            return None
        if line.startswith("data: "):
            data = line[6:]
        elif line.startswith("data:"):
            data = line[5:]
        elif line.startswith("event:"):
            # event 类型行，跳过（通过 data 行的 type 字段判断）
            return None
        else:
            return None

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return None

        chunk_type = chunk.get("type", "")

        if chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                return (text, None)
        elif chunk_type == "message_stop":
            return ("", "__done__")
        elif chunk_type == "message_delta":
            # 可能包含 finish_reason (stop_reason)
            delta = chunk.get("delta", {})
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                return ("", stop_reason)

        return None

    def extract_usage(self, chunk: dict) -> Optional[dict]:
        """提取 Anthropic 格式的 usage。

        Anthropic 的 usage 分散在两个事件中：
        - message_start: { usage: { input_tokens: N } }
        - message_delta: { usage: { output_tokens: N } }
        """
        usage = chunk.get("usage") or chunk.get("message", {}).get("usage")
        if not usage:
            return None

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        # 性能优化7：Anthropic 显式缓存的读/写计量
        # （cache_read 按 ~0.1 倍计费；input_tokens 为未命中部分）
        cache_read = usage.get("cache_read_input_tokens")
        return {
            "total_tokens": input_tokens + output_tokens,
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "cache_read_tokens": cache_read if isinstance(cache_read, int) else None,
        }

    def parse_tool_calls_delta(self, chunk: dict) -> Optional[list[dict]]:
        """从 Anthropic 流式 chunk 中解析 tool_use 增量。

        Anthropic 格式中，tool_use 出现在 content_block_start 事件中：
        {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_xxx","name":"read_file","input":{}}}
        后续的 content_block_delta 事件携带 input_json_delta。
        finish 在 message_delta 事件的 stop_reason="tool_use"。
        """
        chunk_type = chunk.get("type", "")
        if chunk_type == "content_block_start":
            block = chunk.get("content_block", {})
            if block.get("type") == "tool_use":
                # 返回统一格式供调用方累积
                return [{
                    "index": chunk.get("index", 0),
                    "id": block.get("id", ""),
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": "",
                    },
                }]
        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "input_json_delta":
                # 返回 arguments 增量
                return [{
                    "index": chunk.get("index", 0),
                    "id": None,
                    "function": {
                        "name": None,
                        "arguments": delta.get("partial_json", ""),
                    },
                }]
        return None


# ── 适配器工厂 ──
# 对应 DSH 的 adapter 注册机制：根据 provider 的 api_type 选择适配器

_ADAPTERS = {
    "openai": OpenAIAdapter(),
    "anthropic": AnthropicAdapter(),
}


def get_adapter(api_type: str) -> LlmAdapter:
    """根据协议类型获取适配器实例。

    对应 DSH LlmModelDiscoveryRequest.api 字段的概念：
    "Wire protocol the endpoint speaks, when the draft names one."
    """
    adapter = _ADAPTERS.get(api_type or "openai")
    if not adapter:
        raise ValueError(f"不支持的 API 协议类型: {api_type}")
    return adapter


def stream_chat(
    base_url: str,
    api_key: str,
    api_type: str,
    model: str,
    messages: list[dict],
    persona: str,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_file: Optional[Callable] = None,
    parser=None,
    timeout: float = 300.0,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    thinking: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> tuple[str, Optional[int], Optional[int], Optional[int], Optional[str]]:
    """统一的流式对话入口 — 借鉴 DSH adapter.stream() 的设计。

    职责分离：
    - 本函数只管传输层：发送请求、解析 SSE、提取文本和 usage
    - 上下文管理（token 估算、历史选取）由调用方 llm_service 处理
    - 文件标签解析由调用方的 parser 处理

    Returns:
        (full_response, context_used, prompt_tokens, completion_tokens, finish_reason)
    """
    adapter = get_adapter(api_type)
    url = adapter.build_url(base_url)
    headers = adapter.build_headers(api_key)
    payload = adapter.serialize_payload(
        model, messages, persona, temperature, max_tokens,
        thinking=thinking, reasoning_effort=reasoning_effort,
    )

    full_response = ""
    context_used = None
    prompt_tokens = None
    completion_tokens = None
    finish_reason = None

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                # ── 处理流式响应 ──
                # 兼容 "data: " 和 "data:" 两种 SSE 前缀格式
                for line in resp.iter_lines():
                    result = adapter.parse_stream_line(line)
                    if result is None:
                        # 可能包含 usage 信息的行，尝试提取
                        try:
                            if line.startswith("data: "):
                                data = line[6:]
                            elif line.startswith("data:"):
                                data = line[5:]
                            else:
                                continue
                            if data == "[DONE]":
                                break
                            chunk = json.loads(data)
                            usage = adapter.extract_usage(chunk)
                            if usage:
                                context_used = usage.get("total_tokens")
                                prompt_tokens = usage.get("prompt_tokens")
                                completion_tokens = usage.get("completion_tokens")
                        except (json.JSONDecodeError, ValueError):
                            continue
                        continue

                    text, reason = result

                    if reason == "__done__":
                        break

                    if text:
                        if parser:
                            parser.feed(text, on_text=on_chunk, on_file=on_file)
                        else:
                            full_response += text
                            if on_chunk:
                                on_chunk(text)

                    if reason:
                        finish_reason = reason

    except httpx.HTTPStatusError as e:
        # ── 借鉴 DSH adapter.ts 的错误处理 ──
        # 在流式上下文中，e.response 的 body 还没被读取，
        # 需要先 .read() 才能访问 .json() / .text
        error_detail = ""
        try:
            body = e.response.read()
            try:
                error_body = json.loads(body)
                # OpenAI 格式: {"error": {"message": "..."}}
                # Anthropic 格式: {"type": "error", "error": {"type": "...", "message": "..."}}
                error_info = error_body.get("error", {})
                if isinstance(error_info, dict):
                    error_detail = error_info.get("message", "") or error_info.get("type", "")
                else:
                    error_detail = str(error_info)
                if not error_detail:
                    error_detail = error_body.get("detail", "") or str(e)
            except Exception:
                error_detail = body.decode("utf-8", errors="replace")[:500] or str(e)
        except Exception:
            error_detail = str(e)

        logger.error(f"LLM API 错误 (HTTP {e.response.status_code}): {error_detail}")
        raise RuntimeError(f"AI 对话失败 (HTTP {e.response.status_code}): {error_detail}")
    except Exception as e:
        logger.error(f"LLM 流式对话失败: {e}")
        raise RuntimeError(f"AI 对话失败: {e}")

    return full_response, context_used, prompt_tokens, completion_tokens, finish_reason
