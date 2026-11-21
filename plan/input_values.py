"""编号格式与原文边界校验，不承担用户意图识别。"""

import re
from decimal import Decimal, InvalidOperation

from plan.models import ORDER_NUMBER_TOKEN


CALENDAR_DATE = re.compile(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}")
LABELED_NUMBER = re.compile(r"(?:订单(?:号)?\s*[:：]?\s*)?([A-Za-z0-9][A-Za-z0-9_-]{0,63})[。！!]?")


def extract_order_number(text: str) -> str | None:
    # 仅供离线降级接受完整编号，不从任意句子里猜测编号或意图。
    match = LABELED_NUMBER.fullmatch(text.strip().lstrip(":：").strip())
    return match[1] if match and not CALENDAR_DATE.fullmatch(match[1]) else None


def is_original_token(value: str, text: str) -> bool:
    # 验证模型返回的编号是原文完整 token，防止截取长编号中间一段。
    return bool(value and ORDER_NUMBER_TOKEN.fullmatch(value) and re.search(
        rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])", text,
    ))


def amount_from_source(source: str) -> Decimal:
    # 金额可以用中文数词表达，但规范化值必须与原始数字一致，不能让模型另猜一个金额。
    raw = source.strip().removeprefix("人民币").strip().lstrip("￥¥")
    for suffix in ("块钱", "元整", "元", "块"):
        if raw.endswith(suffix):
            raw = raw.removesuffix(suffix).strip()
            break
    try:
        return Decimal(raw)
    except InvalidOperation:
        pass
    # source 可能是“五十元”或“只要退五十元就行”；只定位唯一数值，不识别退款意图。
    # 两个金额、范围和比例不自动取第一项，交回澄清，防止误用否定句里的另一个金额。
    if "半" in raw or "%" in raw or "百分之" in raw:
        raise ValueError("请填写明确金额，不要使用比例。")
    tokens = re.findall(r"[+-]?[0-9]+(?:\.[0-9]+)?|[负正]?[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二三四五六七八九]+)?", raw)
    if len(tokens) != 1:
        raise ValueError("请一次填写一个明确金额。")
    raw = tokens[0]
    try:
        return Decimal(raw)
    except InvalidOperation:
        pass
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    digits.update({"两": 2, "〇": 0})
    whole, separator, fraction = raw.partition("点")
    if not whole or any(ch not in digits and ch not in "十百千万" for ch in whole):
        raise ValueError("请明确填写退款金额，例如 50 元。")
    if any(ch not in digits for ch in fraction) or (separator and not 0 < len(fraction) <= 2):
        raise ValueError("金额最多两位小数。")
    total = section = number = 0
    for char in whole:
        if char in digits:
            number = number * 10 + digits[char]
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
        else:
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            number = 0
    decimal = "." + "".join(str(digits[ch]) for ch in fraction) if fraction else ""
    return Decimal(str(total + section + number) + decimal)
