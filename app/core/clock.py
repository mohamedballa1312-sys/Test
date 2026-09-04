"""Single source of time. Never use date.today() elsewhere — inject a Clock."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, timezone: str = "Asia/Riyadh") -> None:
        self.tz = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()


class FixedClock(Clock):
    """Deterministic clock for tests."""

    def __init__(self, fixed: datetime | date, timezone: str = "Asia/Riyadh") -> None:
        super().__init__(timezone)
        if isinstance(fixed, date) and not isinstance(fixed, datetime):
            fixed = datetime(fixed.year, fixed.month, fixed.day, 12, 0, tzinfo=self.tz)
        self._fixed = fixed if fixed.tzinfo else fixed.replace(tzinfo=self.tz)

    def now(self) -> datetime:
        return self._fixed
