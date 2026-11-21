"""Five-field cron lines the way GitHub's `schedule` reads them (UTC), and the last slot at or before a time.

Supported in each field: `*`, a number, `A-B`, `*/N`, `A-B/N`, and comma lists of those. Everything else (names
such as MON or JAN, `?`, `L`, `W`, `#`, `@daily`, six fields, numbers out of range) is refused with the reason:
a line this module would read differently from GitHub must never pass.

Day of month and day of week: when both are restricted (neither starts with `*`), a day matches if either does
(POSIX cron; GitHub does the same).
"""
import datetime, re

UTC = datetime.timezone.utc
FIELDS = (('minute', 0, 59), ('hour', 0, 23), ('day of month', 1, 31), ('month', 1, 12), ('day of week', 0, 6))
PART_RE = re.compile(r'^(\*|(\d{1,2})(?:-(\d{1,2}))?)(?:/(\d{1,2}))?\Z')
SEARCH_DAYS = 366 * 9       # 29 February comes at least once in any 9 years (1900-style gaps included)
START = datetime.date(2024, 1, 1)


class CronError(ValueError):
    pass


def _field(text, name, lo, hi):
    values = set()
    for part in text.split(','):
        m = PART_RE.match(part)
        if not m:
            raise CronError(f'{name} {text!r}: only numbers, *, A-B, */N, A-B/N and comma lists are supported')
        star, a, b, step = m.group(1) == '*', m.group(2), m.group(3), m.group(4)
        if star:
            first, last = lo, hi
        elif step is not None and b is None:
            raise CronError(f'{name} {text!r}: a step needs * or a range before it (*/N or A-B/N), not {part!r}')
        else:
            first = int(a)
            last = int(b) if b is not None else first
        if not (lo <= first <= hi and lo <= last <= hi):
            raise CronError(f'{name} {text!r}: values must be {lo}-{hi}')
        if first > last:
            raise CronError(f'{name} {text!r}: a range must go up ({first}-{last})')
        n = int(step) if step is not None else 1
        if n < 1:
            raise CronError(f'{name} {text!r}: a step must be 1 or more')
        values.update(range(first, last + 1, n))
    return frozenset(values)


class Cron:
    def __init__(self, line):
        if not isinstance(line, str):
            raise CronError('a cron line must be a string')
        parts = line.split()
        if len(parts) != 5:
            raise CronError(f'{line!r}: a cron line has exactly 5 fields (minute hour day-of-month month '
                            'day-of-week)')
        self.line = ' '.join(parts)
        self.minutes, self.hours, self.doms, self.months, self.dows = (
            _field(text, name, lo, hi) for text, (name, lo, hi) in zip(parts, FIELDS))
        self.dom_star, self.dow_star = parts[2].startswith('*'), parts[4].startswith('*')
        self.hours_desc = sorted(self.hours, reverse=True)
        self.minutes_desc = sorted(self.minutes, reverse=True)

    def day_matches(self, day):
        if day.month not in self.months:
            return False
        dom = day.day in self.doms
        dow = (day.weekday() + 1) % 7 in self.dows          # cron: 0 is Sunday; Python: 0 is Monday
        if self.dom_star or self.dow_star:
            return dom and dow
        return dom or dow

    def matches(self, when):
        return when.minute in self.minutes and when.hour in self.hours and self.day_matches(when.date())

    def last_slot(self, now):
        """The latest minute at or before `now` (an aware time) that the line names, in UTC; None if there is
        none in the last nine years (a line such as `0 0 31 2 *` that never comes)."""
        now = now.astimezone(UTC).replace(second=0, microsecond=0)
        today = now.date()
        for back in range(SEARCH_DAYS):
            day = today - datetime.timedelta(days=back)
            if not self.day_matches(day):
                continue
            for hour in self.hours_desc:
                if back == 0 and hour > now.hour:
                    continue
                for minute in self.minutes_desc:
                    if back == 0 and hour == now.hour and minute > now.minute:
                        continue
                    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
        return None

    def min_gap(self):
        """The fewest minutes between two slots in a row: within a day, or from a day's last slot to the next
        day's first when two days in a row both match (looked for over nine years from a fixed day)."""
        times = sorted(h * 60 + m for h in self.hours for m in self.minutes)
        gaps = [b - a for a, b in zip(times, times[1:])]
        days = [self.day_matches(START + datetime.timedelta(days=n)) for n in range(SEARCH_DAYS + 1)]
        if any(a and b for a, b in zip(days, days[1:])):
            gaps.append(24 * 60 - times[-1] + times[0])
        return min(gaps, default=None)
