import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Self

import httpx

from app.config import Settings


type TravelData = dict[str, Any]
PRODUCT_PATHS = {
    "hotel": ("hotels", "hotels"),
    "scenic": ("scenic-spots", "scenic-spots"),
    "flight": ("flights/search", "flights"),
    "train": ("trains/search", "trains"),
    "bus": ("buses/search", "buses"),
    "transfer": ("transfers", "transfers"),
}


class TravelClientError(RuntimeError):
    """客服调用旅游中台失败。"""


class TravelTimeoutError(TravelClientError):
    """连接、读取或整次请求超时。"""


class TravelTransportError(TravelClientError):
    """未能收到中台的 HTTP 响应。"""


class TravelHTTPError(TravelClientError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"旅游中台返回 HTTP {status_code}")


class TravelResponseError(TravelClientError):
    """中台成功响应不是约定的 JSON 对象。"""


def _require_positive_id(value: int, name: str) -> int:
    # 中台 ID 是正整数；不允许把任意文本拼进请求路径或身份头。
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


class TravelClient:
    """旅游中台只读客户端；返回原始业务字段，供后续客服流程消费。"""

    def __init__(self, http_client: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    @classmethod
    @asynccontextmanager
    async def from_settings(cls, settings: Settings | None = None) -> AsyncIterator[Self]:
        config = settings if settings is not None else Settings()
        if not config.travel_api_base_url:
            raise ValueError("请先配置 TRAVEL_API_BASE_URL")
        url = httpx.URL(config.travel_api_base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.query or url.fragment or url.userinfo:
            raise ValueError("TRAVEL_API_BASE_URL 必须是无用户名、口令、查询参数或片段的 HTTP(S) 地址")

        path = url.path.rstrip("/")
        if not path.endswith("/api/v1"):
            path += "/api/v1"
        base_url = url.copy_with(path=f"{path}/")
        # 一个生命周期内复用连接；退出上下文时自动关闭。
        async with httpx.AsyncClient(base_url=base_url, headers={"Accept": "application/json"}) as http_client:
            yield cls(http_client, config.travel_api_timeout_seconds)

    async def get_me(self, user_id: int) -> TravelData:
        return await self._get("me", user_id)

    async def list_orders(
        self,
        user_id: int,
        *,
        page_no: int = 1,
        page_size: int = 20,
        status_code: str | None = None,
        order_type_code: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
    ) -> TravelData:
        return await self._get(
            "orders",
            user_id,
            {
                "pageNo": page_no,
                "pageSize": page_size,
                "statusCode": status_code,
                "orderTypeCode": order_type_code,
                "createdFrom": created_from,
                "createdTo": created_to,
            },
        )

    async def get_order(self, user_id: int, order_id: int) -> TravelData:
        return await self._get(f"orders/{_require_positive_id(order_id, 'order_id')}", user_id)

    async def list_order_payments(self, user_id: int, order_id: int) -> TravelData:
        return await self._get(f"orders/{_require_positive_id(order_id, 'order_id')}/payments", user_id)

    async def list_order_refund_records(self, user_id: int, order_id: int) -> TravelData:
        return await self._get(f"orders/{_require_positive_id(order_id, 'order_id')}/refund-records", user_id)

    async def list_refund_requests(
        self,
        user_id: int,
        *,
        page_no: int = 1,
        page_size: int = 20,
        status_code: str | None = None,
        requested_from: str | None = None,
        requested_to: str | None = None,
    ) -> TravelData:
        return await self._get(
            "refund-requests",
            user_id,
            {
                "pageNo": page_no,
                "pageSize": page_size,
                "statusCode": status_code,
                "requestedFrom": requested_from,
                "requestedTo": requested_to,
            },
        )

    async def get_refund_request(self, user_id: int, refund_request_id: int) -> TravelData:
        request_id = _require_positive_id(refund_request_id, "refund_request_id")
        return await self._get(f"refund-requests/{request_id}", user_id)

    async def create_refund_request(
        self, user_id: int, order_id: int, item_id: int, amount: float, reason: str,
    ) -> TravelData:
        order_id = _require_positive_id(order_id, "order_id")
        item_id = _require_positive_id(item_id, "item_id")
        # 这是唯一的中台写接口，底层只发送一次；是否已提交由上层持久化并核对。
        return await self._request(f"orders/{order_id}/items/{item_id}/refund-requests", user_id, method="POST",
                                   body={"requestedAmount": amount, "reason": reason})

    async def search_products(self, kind: str, params: dict[str, str | int]) -> TravelData:
        return await self._get(PRODUCT_PATHS[kind][0], None, params)

    async def get_product(self, kind: str, product_id: int) -> TravelData:
        product_id = _require_positive_id(product_id, "product_id")
        return await self._get(f"{PRODUCT_PATHS[kind][1]}/{product_id}", None)

    async def get_hotel_room_types(self, hotel_id: int, check_in: str, check_out: str) -> TravelData:
        hotel_id = _require_positive_id(hotel_id, "hotel_id")
        return await self._get(
            f"hotels/{hotel_id}/room-types", None, {"checkInDate": check_in, "checkOutDate": check_out}
        )

    async def get_scenic_ticket_types(self, spot_id: int, travel_date: str) -> TravelData:
        spot_id = _require_positive_id(spot_id, "spot_id")
        return await self._get(f"scenic-spots/{spot_id}/ticket-types", None, {"travelDate": travel_date})

    async def get_transfer_price(self, service_id: int, pickup: int, dropoff: int, business_date: str) -> TravelData:
        service_id = _require_positive_id(service_id, "service_id")
        return await self._get(f"transfers/{service_id}/pricing", None, {
            "pickupAreaId": pickup, "dropoffAreaId": dropoff, "businessDate": business_date,
        })

    async def _get(
        self,
        path: str,
        user_id: int | None,
        params: Mapping[str, str | int | None] | None = None,
    ) -> TravelData:
        return await self._request(path, user_id, params=params)

    async def _request(
        self, path: str, user_id: int | None, *, method: str = "GET",
        params: Mapping[str, str | int | None] | None = None, body: dict | None = None,
    ) -> TravelData:
        # 公开商品接口不需要用户标识；订单等私有接口仍随请求传入。
        headers = {} if user_id is None else {"X-User-Id": str(_require_positive_id(user_id, "user_id"))}
        query = {key: value for key, value in (params or {}).items() if value is not None}
        try:
            # HTTPX 限制单个 I/O 阶段，asyncio.timeout 额外限制整次请求。
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._http_client.request(
                    method,
                    path,
                    headers=headers,
                    params=query,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    json=body,
                )
        except (TimeoutError, httpx.TimeoutException):
            raise TravelTimeoutError("旅游中台请求超时") from None
        except httpx.RequestError:
            raise TravelTransportError("无法连接或读取旅游中台") from None

        # 404、5xx 等不能当成空列表；异常也不包含可能有个人信息的响应正文。
        if not response.is_success:
            raise TravelHTTPError(response.status_code)
        try:
            result = response.json()
        except ValueError:
            raise TravelResponseError("旅游中台返回了无效的 JSON") from None
        if not isinstance(result, dict):
            raise TravelResponseError("旅游中台响应应为 JSON 对象")
        return result
