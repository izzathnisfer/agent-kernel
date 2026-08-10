"""Validation and interpretation of a ``ScheduleSpec``'s timing expression.

Provider-agnostic: the minimum granularity is supplied by the caller (a ``Scheduler``
exposes its own), so nothing here knows which timer will run the schedule.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from .errors import ScheduleValidationError
from .model import ScheduleSpec

# "<n> <unit>" — the rate grammar every supported timer shares.
_RATE_PATTERN = re.compile(r"^\s*(\d+)\s+(minute|minutes|hour|hours|day|days|second|seconds)\s*$", re.IGNORECASE)

_RATE_UNITS = {
    "second": timedelta(seconds=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
}

# A cron expression has six fields (minute hour day-of-month month day-of-week year).
# A seventh leading field would be seconds — finer than any supported timer, and silently
# rounding it is exactly what this validation exists to prevent.
_CRON_FIELD_COUNT = 6


class ScheduleExpression:
    """Reads a ``ScheduleSpec``'s timing expression."""

    @staticmethod
    def validate(spec: ScheduleSpec, minimum_granularity: timedelta, now: Optional[datetime] = None) -> None:
        """Reject a schedule that cannot be registered as written.

        :param spec: The schedule to validate.
        :param minimum_granularity: The finest interval the target timer supports.
        :param now: Reference time for the one-time in-the-future check; defaults to UTC now.
        :raises ScheduleValidationError: The expression is malformed, too fine, or in the past.
        """
        if spec.rate is not None:
            interval = ScheduleExpression.parse_rate(spec.rate)
            if interval < minimum_granularity:
                raise ScheduleValidationError(f"rate '{spec.rate}' is finer than the minimum granularity of {minimum_granularity}")
            return

        if spec.cron is not None:
            ScheduleExpression._validate_cron(spec.cron)
            return

        reference = now or datetime.now(timezone.utc)
        if ScheduleExpression.as_utc(spec.at) <= reference:
            raise ScheduleValidationError(f"one-time schedule at '{spec.at.isoformat()}' is not in the future")

    @staticmethod
    def parse_rate(rate: str) -> timedelta:
        """Parse a ``"<n> <unit>"`` rate into its interval.

        :param rate: The rate expression.
        :return: The interval between fires.
        :raises ScheduleValidationError: The expression does not match the rate grammar.
        """
        match = _RATE_PATTERN.match(rate or "")
        if match is None:
            raise ScheduleValidationError(f"rate '{rate}' is not of the form '<n> <minute|hour|day>'")
        amount = int(match.group(1))
        if amount < 1:
            raise ScheduleValidationError(f"rate '{rate}' must specify a positive interval")
        unit = match.group(2).lower().rstrip("s")
        return _RATE_UNITS[unit] * amount

    @staticmethod
    def next_run_at(spec: ScheduleSpec, created_at: datetime) -> Optional[datetime]:
        """Derive the next fire time, where it is knowable without evaluating the expression.

        Best-effort by design: no timer API supplies a next-invocation time, and adding a
        cron evaluator for a convenience field is not worth the dependency. ``None`` means
        "not computed", never "not scheduled" — authoritative history is ``last_run_at``.

        :param spec: The schedule to read.
        :param created_at: When the schedule was registered, the base for a rate.
        :return: The next fire time in UTC, or None for a cron expression.
        """
        if spec.at is not None:
            return ScheduleExpression.as_utc(spec.at)
        if spec.rate is not None:
            return ScheduleExpression.as_utc(created_at) + ScheduleExpression.parse_rate(spec.rate)
        return None

    @staticmethod
    def is_one_time(spec: ScheduleSpec) -> bool:
        """Whether the schedule fires once and then completes.

        :param spec: The schedule to read.
        :return: True for an ``at`` schedule.
        """
        return spec.at is not None

    @staticmethod
    def as_utc(moment: datetime) -> datetime:
        """Normalize a datetime to UTC, treating a naive value as already UTC.

        :param moment: The datetime to normalize.
        :return: The same instant with a UTC timezone.
        """
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    @staticmethod
    def _validate_cron(cron: str) -> None:
        """Reject a cron expression whose field count no supported timer accepts.

        :param cron: The cron expression, without the surrounding ``cron(...)``.
        :raises ScheduleValidationError: The expression does not have six fields.
        """
        fields = cron.split()
        if len(fields) != _CRON_FIELD_COUNT:
            raise ScheduleValidationError(
                f"cron '{cron}' has {len(fields)} fields; expected {_CRON_FIELD_COUNT} "
                "(minute hour day-of-month month day-of-week year). Sub-minute schedules are not supported."
            )
