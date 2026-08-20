from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_today() -> date:
    return datetime.now(SHANGHAI).date()


def utc_day_bounds(day: date | None = None) -> tuple[datetime, datetime]:
    target = day or local_today()
    start = datetime.combine(target, time.min, tzinfo=SHANGHAI).astimezone(UTC)
    return start, start + timedelta(days=1)
