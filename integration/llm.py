import asyncio

import httpx

from app.config import Settings


class ModelUnavailableError(RuntimeError):
    """模型请求失败；调用方可以改用本地规则。"""


class InvalidModelOutputError(ValueError):
    """模型响应不是可解析的计划。"""


class LlmClient:
    """用现有 HTTPX 客户端调用 Chat Completions 兼容接口。"""

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        if not (settings.llm_base_url and settings.llm_model and settings.llm_api_key):
            raise ValueError("模型地址、名称和密钥必须一起配置")
        base_url = httpx.URL(settings.llm_base_url)
        if (
            base_url.scheme not in {"http", "https"} or not base_url.host
            or base_url.userinfo or base_url.query or base_url.fragment
        ):
            raise ValueError("LLM_BASE_URL 必须是有效的模型服务地址")
        path = base_url.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions"
        self._url = base_url.copy_with(path=path)
        self._model = settings.llm_model
        self._api_key = settings.llm_api_key
        self._timeout = settings.llm_timeout_seconds
        self._http = http_client

    async def propose(self, system_prompt: str, context: str) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": context}],
            "response_format": {"type": "json_object"},
            "max_tokens": 256,
        }
        if self._url.host.endswith(".aliyuncs.com") and "/compatible-mode/" in self._url.path:
            # 百炼的 JSON 模式要求关闭思考；其他兼容服务不必接受这个扩展字段。
            payload["enable_thinking"] = False
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._http.post(
                    self._url, headers={"Authorization": f"Bearer {self._api_key.get_secret_value()}"},
                    json=payload, timeout=self._timeout, follow_redirects=False,
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ModelUnavailableError(f"模型服务返回 HTTP {error.response.status_code}") from None
        except (httpx.RequestError, TimeoutError):
            raise ModelUnavailableError("模型服务连接失败或超时") from None

        # 不把原始响应和鉴权头写到日志，也不把模型的解释文字发给用户。
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise InvalidModelOutputError("模型响应缺少计划正文") from None
        if not isinstance(content, str) or not content.strip() or len(content) > 4096:
            raise InvalidModelOutputError("模型计划正文无效")
        return content
