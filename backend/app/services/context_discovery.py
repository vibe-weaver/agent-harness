"""DSH 风格模型自动发现 — 移植自 dsh-llm-pi-ai/discovery.ts。

严格对应 DSH 桌面端源码的 discoverModels() 函数:
  1. 调用 Provider 的 GET /models 端点（OpenAI 兼容协议）
  2. 解析返回的 {data: [...]} 数组
  3. 从每个条目中提取 id、name、context_window/context_length、max_tokens/max_output_tokens
  4. 返回 LlmDiscoveredModel 列表

管理员添加模型时，可调用此服务自动探测可用模型及其上下文窗口大小，
无需手动填写 context_length。

设计要点（与 DSH 一致）:
- 只有 OpenAI 兼容协议才可探测（GET /models + Bearer auth）
- 响应体大小限制 4MB，防止恶意/超大响应
- 单条目解析失败不影响其他条目
- context_window 和 context_length 都尝试读取（不同 Provider 命名不同）
- 探测时可以使用已有的 API Key，也可以无 Key 探测（部分 Provider 允许）
"""

import logging
import ssl
from dataclasses import dataclass
from typing import Optional

import httpx


def _tls_compat_ctx() -> ssl.SSLContext:
    """强制 TLS 1.2 的 SSL 上下文。"""
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


logger = logging.getLogger(__name__)

# 响应体大小上限 — 对应 DSH 的 MAX_RESPONSE_BYTES
MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB

# 请求超时 — 对应 DSH 的 fetch 超时
DISCOVERY_TIMEOUT = 15.0

# 模型连通性测试超时
CHAT_TEST_TIMEOUT = 20.0


@dataclass
class DiscoveredModel:
    """一个被探测到的模型 — 对应 DSH 的 LlmDiscoveredModel。"""
    id: str
    name: Optional[str] = None
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None

    def to_dict(self) -> dict:
        d = {"id": self.id}
        if self.name:
            d["name"] = self.name
        if self.context_window:
            d["context_window"] = self.context_window
        if self.max_tokens:
            d["max_tokens"] = self.max_tokens
        return d


def _capacity(*candidates) -> Optional[int]:
    """从多个候选字段中提取一个正整数 — 对应 DSH 的 capacity()。"""
    for candidate in candidates:
        if isinstance(candidate, (int, float)) and candidate == int(candidate) and candidate > 0:
            return int(candidate)
    return None


def _label(*candidates) -> Optional[str]:
    """从多个候选字段中提取一个非空字符串 — 对应 DSH 的 label()。"""
    for candidate in candidates:
        if isinstance(candidate, str) and len(candidate) > 0:
            return candidate
    return None


def _listing_url(base_url: str) -> str:
    """拼接 GET /models 端点 URL — 对应 DSH 的 listingUrl()。

    将 base_url 视为前缀而非完整 URL，保留路径段。
    """
    return f"{base_url.rstrip('/')}/models"


def _read_listing(body: dict) -> list[DiscoveredModel]:
    """解析 OpenAI 兼容的 GET /models 响应 — 对应 DSH 的 readListing()。

    响应格式: {"data": [{"id": "...", "name": "...", "context_window": ...}, ...]}
    没有 id 的条目会被跳过，不会导致整体失败。
    """
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError(
            '端点的模型列表没有 "data" 数组；请手动输入此厂商的模型'
        )

    models: list[DiscoveredModel] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        entry = raw
        model_id = _label(entry.get("id"))
        if model_id is None:
            continue
        name = _label(entry.get("name"), entry.get("display_name"))
        context_window = _capacity(
            entry.get("context_window"),
            entry.get("context_length"),
        )
        max_tokens = _capacity(
            entry.get("max_output_tokens"),
            entry.get("max_tokens"),
        )
        models.append(DiscoveredModel(
            id=model_id,
            name=name,
            context_window=context_window,
            max_tokens=max_tokens,
        ))

    return models


async def discover_models(
    base_url: str,
    api_key: Optional[str] = None,
) -> list[DiscoveredModel]:
    """探测一个 Provider 端点的可用模型列表 — 对应 DSH 的 discoverModels()。

    Args:
        base_url: Provider 的 API 端点基址，如 https://api.deepseek.com
        api_key: 可选的 API Key，用于认证。部分 Provider 允许无 Key 探测。

    Returns:
        探测到的模型列表，每个模型包含 id、name、context_window、max_tokens。

    Raises:
        RuntimeError: 当端点不可达、返回非 200、或响应格式不正确时。
    """
    url = _listing_url(base_url)
    headers = {"accept": "application/json"}
    if api_key:
        api_key = api_key.strip()
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(DISCOVERY_TIMEOUT, connect=8.0), verify=_tls_compat_ctx()) as client:
            response = await client.get(url, headers=headers)

            if response.status_code == 401 or response.status_code == 403:
                raise RuntimeError(
                    f"{url} 返回 {response.status_code}；请检查 API Key 是否正确"
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"{url} 返回 HTTP {response.status_code}"
                )

            # 检查 Content-Length，拒绝超大响应
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > MAX_RESPONSE_BYTES:
                        raise RuntimeError(
                            f"{url} 返回的数据超过 {MAX_RESPONSE_BYTES // 1024 // 1024}MB 限制"
                        )
                except ValueError:
                    pass

            # 读取响应体，检查实际大小
            body_bytes = response.content
            if len(body_bytes) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"{url} 返回的数据超过 {MAX_RESPONSE_BYTES // 1024 // 1024}MB 限制"
                )

            # 解析 JSON
            try:
                body = response.json()
            except Exception as e:
                raise RuntimeError(f"{url} 未返回有效的 JSON: {e}")

            # 解析模型列表
            models = _read_listing(body)

            logger.info(
                f"discover_models: 从 {url} 探测到 {len(models)} 个模型"
            )
            return models

    except httpx.ConnectError as e:
        raise RuntimeError(f"无法连接到 {url}: {e}")
    except httpx.TimeoutException:
        raise RuntimeError(f"连接 {url} 超时（{DISCOVERY_TIMEOUT}s）")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"探测 {url} 失败: {e}")


def discover_models_sync(
    base_url: str,
    api_key: Optional[str] = None,
) -> list[DiscoveredModel]:
    """同步版本的 discover_models — 在子线程中调用。

    逻辑与异步版本完全一致，使用 httpx.Client 替代 AsyncClient。
    """
    url = _listing_url(base_url)
    headers = {"accept": "application/json"}
    if api_key:
        api_key = api_key.strip()
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(timeout=httpx.Timeout(DISCOVERY_TIMEOUT, connect=8.0), verify=_tls_compat_ctx()) as client:
            response = client.get(url, headers=headers)

            if response.status_code == 401 or response.status_code == 403:
                raise RuntimeError(
                    f"{url} 返回 {response.status_code}；请检查 API Key 是否正确"
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"{url} 返回 HTTP {response.status_code}"
                )

            # 检查 Content-Length，拒绝超大响应
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    cl = int(content_length)
                    if cl > MAX_RESPONSE_BYTES:
                        raise RuntimeError(
                            f"{url} 返回的数据超过 {MAX_RESPONSE_BYTES // 1024 // 1024}MB 限制"
                        )
                except ValueError:
                    pass

            # 读取响应体，检查实际大小
            body_bytes = response.content
            if len(body_bytes) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"{url} 返回的数据超过 {MAX_RESPONSE_BYTES // 1024 // 1024}MB 限制"
                )

            # 解析 JSON
            try:
                body = response.json()
            except Exception as e:
                raise RuntimeError(f"{url} 未返回有效的 JSON: {e}")

            # 解析模型列表
            models = _read_listing(body)

            logger.info(
                f"discover_models_sync: 从 {url} 探测到 {len(models)} 个模型"
            )
            return models

    except httpx.ConnectError as e:
        raise RuntimeError(f"无法连接到 {url}: {e}")
    except httpx.TimeoutException:
        raise RuntimeError(f"连接 {url} 超时（{DISCOVERY_TIMEOUT}s）")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"探测 {url} 失败: {e}")


async def test_chat_model(
    base_url: str,
    api_key: Optional[str],
    model_name: str,
) -> str:
    """向 Provider 发送最小 chat 请求，验证模型标识是否真实可用（B1 测试连接）。

    发送 {"model": name, "messages": [{"role":"user","content":"ping"}], "max_tokens": 1}，
    仅用于验证配置，不产生有意义的生成内容。

    Args:
        base_url: Provider 的 API 端点基址
        api_key: 可选的 API Key
        model_name: 待验证的模型标识

    Returns:
        成功描述字符串。

    Raises:
        RuntimeError: 当端点不可达、超时、返回非 200、或模型不存在时。
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"accept": "application/json", "content-type": "application/json"}
    if api_key:
        api_key = api_key.strip()
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(CHAT_TEST_TIMEOUT, connect=8.0)) as client:
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code == 200:
                return f"模型 {model_name} 可用（HTTP 200）"

            # 提取服务端返回的错误信息（OpenAI 兼容格式 error.message）
            detail = ""
            try:
                body = response.json()
                error = body.get("error")
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("code") or ""
                elif error:
                    detail = str(error)
            except Exception:
                pass
            if not detail and response.text:
                detail = response.text[:200]
            raise RuntimeError(f"{url} 返回 HTTP {response.status_code}: {detail}")

    except httpx.ConnectError as e:
        raise RuntimeError(f"无法连接到 {url}: {e}")
    except httpx.TimeoutException:
        raise RuntimeError(f"连接 {url} 超时（{CHAT_TEST_TIMEOUT}s）")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"测试 {url} 失败: {e}")
