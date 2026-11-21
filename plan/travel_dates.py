import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RELATIVE = {"前天": -2, "昨天": -1, "今天": 0, "明天": 1, "后天": 2, "大后天": 3}
WEEKDAYS = "一二三四五六日"
WEEK_PATTERN = re.compile(r"(下下|上上|本|这|下|上)?(?:周|星期)([一二三四五六日天])")
FULL_DATE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
CHINESE_DATE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]?")


def business_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows 无 IANA 数据时，现代中国行程仍可使用固定 UTC+8，不取机器本地时区。
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name)
        if name == "UTC":
            return UTC
        raise ValueError(f"找不到业务时区 {name}；请安装 IANA 时区数据或配置 Asia/Shanghai") from None


def calendar_today(zone: tzinfo) -> date:
    return datetime.now(UTC).astimezone(zone).date()


def parse_date_text(text: str, today: date) -> date:
    # “下周一”以本周周一为锚点，不是简单地在现在加七天。
    if text in RELATIVE:
        return today + timedelta(days=RELATIVE[text])
    if match := WEEK_PATTERN.fullmatch(text):
        prefix, weekday = match.groups()
        position = WEEKDAYS.index("日" if weekday == "天" else weekday)
        weeks = {"上上": -2, "上": -1, "本": 0, "这": 0, "下": 1, "下下": 2, None: 0}[prefix]
        days = weeks * 7 + position - today.weekday()
        if prefix is None and days < 0:
            days += 7
        return today + timedelta(days=days)
    if match := FULL_DATE.fullmatch(text):
        return date(*(int(value) for value in match.groups()))
    if match := CHINESE_DATE.fullmatch(text):
        year, month, day = match.groups()
        return date(int(year) if year else today.year, int(month), int(day))
    raise ValueError("未识别的日期")


def upstream_travel_date(raw: object, zone: tzinfo) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        timestamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # 中台无时区的时间已经是业务本地时间；带时区的时间先转换再比较日期。
    return (timestamp.astimezone(zone) if timestamp.tzinfo is not None else timestamp).date()
