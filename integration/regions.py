import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}
GENERIC_NAMES = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}
SUFFIX = re.compile(r"(?:特别行政区|自治区|自治州|自治县|地区|市|区|县|盟|旗)$")


@dataclass(frozen=True)
class Area:
    id: int
    name: str
    full_name: str
    level: int

    @property
    def label(self) -> str:
        return " / ".join(part for part in self.full_name.split("/") if part not in GENERIC_NAMES)


def _clean(text: str) -> str:
    return re.sub(r"[\s/／]+", "", text)


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
        # 优先匹配长名字，例如“北京市朝阳区”不能先被截成“北京”。
        keys = sorted(self._names, key=lambda key: (-len(key), key))
        self._mention_pattern = re.compile("|".join(re.escape(key) for key in keys))

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
        return tuple(self._names.get(_clean(name), {}).values())

    def mentions(self, text: str) -> list[str]:
        # 同城接送可能同时把“北京”写成上车地和下车地，保留出现顺序及重复项。
        return [match[0] for match in self._mention_pattern.finditer(text)]
