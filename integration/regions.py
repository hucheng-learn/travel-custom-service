import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}
GENERIC_NAMES = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}
SUFFIX = re.compile(r"(?:特别行政区|自治区|自治州|自治县|地区|市|区|县|盟|旗)$")
AREA_SEPARATORS = re.compile(r"[\s/／]+|下辖的?|下的")


@dataclass(frozen=True)
class Area:
    id: int
    name: str
    full_name: str
    level: int

    @property
    def label(self) -> str:
        return " / ".join(part for part in self.full_name.split("/") if part not in GENERIC_NAMES)

    def matches_district_address(self, address: str) -> bool:
        # 地址可能省略省市，但区县必须出现在行政区划位置，避免把道路里的同名词当成区县。
        address = re.sub(r"[\s/／,，]+", "", address)
        for parent in self.full_name.split("/")[:-1]:
            if parent not in GENERIC_NAMES and parent != self.name:
                address = address.removeprefix(parent)
        return address.startswith(self.name)


def _clean(text: str) -> str:
    return AREA_SEPARATORS.sub("", text)


def _short(text: str) -> str:
    return SUFFIX.sub("", text)


class AreaCatalog:
    def __init__(self, path: Path) -> None:
        self._names: dict[str, dict[int, Area]] = defaultdict(dict)
        with path.open(encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                if row.get("status_code", "active") != "active":
                    continue
                area = Area(int(row["id"]), row["area_name"], row["area_full_name"], int(row["level"]))
                self._index(area)
        if not self._names:
            raise ValueError("地区映射没有可用条目")

    def _index(self, area: Area) -> None:
        parts = [part for part in area.full_name.split("/") if part not in GENERIC_NAMES]
        if area.level == 1 and area.name in MUNICIPALITIES:
            return  # 商品接口实际使用其下的“市辖区”城市节点，而不是省级节点。
        if not parts:
            return
        if area.name in GENERIC_NAMES and not (area.level == 2 and parts[0] in MUNICIPALITIES):
            return
        aliases = {area.full_name, area.name, parts[-1], _short(parts[-1])} - GENERIC_NAMES
        for start in range(len(parts)):
            aliases.add("".join(parts[start:]))
            aliases.add("".join(_short(part) for part in parts[start:]))
            aliases.add("".join(_short(part) for part in parts[start:-1]) + parts[-1])
        for alias in aliases:
            if len(key := _clean(alias)) >= 2:
                self._names[key][area.id] = area

    def resolve(self, name: str) -> tuple[Area, ...]:
        # 返回所有同名候选，让对话明确询问；不默认选第一个。
        exact = self._names.get(_clean(name))
        if exact:
            return tuple(exact.values())
        # 省名和区名之间可能省略了市名，例如“江苏省 / 鼓楼区”。
        parent, separator, district = name.replace("／", "/").rpartition("/")
        if separator:
            return self._within(self.resolve(district), self.resolve(parent))
        return ()

    def city_for(self, area: Area) -> Area:
        # 使用 CSV 的完整上级路径，直辖市也会落到接口使用的“市辖区”节点。
        if area.level <= 2:
            return area
        city_path = area.full_name.rpartition("/")[0]
        return next((city for city in self.resolve(city_path) if city.level == 2 and city.full_name == city_path), area)

    @staticmethod
    def _within(areas: tuple[Area, ...], parents: tuple[Area, ...]) -> tuple[Area, ...]:
        return tuple(area for area in areas if any(area.full_name.startswith(parent.full_name + "/") for parent in parents))

    def refine(self, name: str, reply: str) -> str:
        candidates, scopes = self.resolve(name), self.resolve(reply)
        if len(candidates) < 2 or not scopes or max(area.level for area in scopes) >= min(area.level for area in candidates):
            return reply
        # 等待消歧时，省市回复只用于缩小原区县候选，不能直接拿省市 ID 查商品。
        narrowed = self._within(candidates, scopes)
        if len(narrowed) == 1:
            return narrowed[0].label
        if len(scopes) == 1 and 1 < len(narrowed) < len(candidates):
            scoped_name = f"{scopes[0].label} / {narrowed[0].name}"
            if self.resolve(scoped_name) == narrowed:
                return scoped_name
        return name
