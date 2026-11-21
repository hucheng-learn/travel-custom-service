from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


type ProductKind = Literal["hotel", "scenic", "flight", "train", "bus", "transfer"]
TRANSPORT_KINDS = {"flight", "train", "bus"}
PRODUCT_NAMES = {
    "hotel": "酒店", "scenic": "景点", "flight": "机票",
    "train": "火车票", "bus": "汽车票", "transfer": "接送",
}
HOTEL_TYPE_NAMES = {"luxury": "豪华型", "business": "商务型", "resort": "度假型", "boutique": "精品型"}


class ProductClues(BaseModel):
    # 规划器只能提取原文片段，不能指定地区 ID、商品 ID 或价格。
    model_config = ConfigDict(extra="forbid")
    kind: ProductKind | None = None
    area: str | None = Field(None, max_length=80)
    origin: str | None = Field(None, max_length=80)
    destination: str | None = Field(None, max_length=80)
    date_text: str | None = Field(None, max_length=20)
    end_date_text: str | None = Field(None, max_length=20)
    date_value: date | None = None
    end_date_value: date | None = None
    keyword: str | None = Field(None, max_length=80)
    hotel_star_rating: Literal["3", "4", "5"] | None = None
    hotel_star_text: str | None = Field(None, max_length=40)
    hotel_type: Literal["luxury", "business", "resort", "boutique"] | None = None
    hotel_type_text: str | None = Field(None, max_length=40)
    detail: bool = False
    quote: bool = False


class ProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ProductKind | None = None
    area: str | None = None
    origin: str | None = None
    destination: str | None = None
    business_date: date | None = None
    end_date: date | None = None
    keyword: str | None = None
    hotel_star_rating: Literal["3", "4", "5"] | None = None
    hotel_type: Literal["luxury", "business", "resort", "boutique"] | None = None
    detail: bool = False
    quote: bool = False


class DateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    travel_date: date
