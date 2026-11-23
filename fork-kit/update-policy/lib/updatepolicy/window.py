"""The quiet window: from quietHour, for windowMinutes, in the machine file's time zone. Pure; every time is an aware
datetime, so a test hands in any clock."""
import datetime, zoneinfo

UTC = datetime.timezone.utc


class Window:
    def __init__(self, start, end, zone):
        self.start, self.end, self.zone = start, end, zone

    @property
    def id(self):
        """The window's local start date: one window per day, whatever the clock does."""
        return self.start.astimezone(self.zone).date().isoformat()

    def text(self):
        a, b = self.start.astimezone(self.zone), self.end.astimezone(self.zone)
        return f'{a:%H:%M}-{b:%H:%M} {self.zone.key}'


def _start_on(day, hour, minute, zone):
    """The window's start on one local day, in UTC. A start time the clock skips (a DST change) moves later by the
    length of the gap; one the clock goes through twice is the first of the two."""
    return datetime.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)


def windows_around(now, settings):
    """Yesterday's, today's and tomorrow's windows (local days), in order."""
    zone = zoneinfo.ZoneInfo(settings['timeZone'])
    hour, minute = (int(x) for x in settings['quietHour'].split(':'))
    today = now.astimezone(zone).date()
    length = datetime.timedelta(minutes=settings['windowMinutes'])
    out = []
    for d in (-1, 0, 1):
        start = _start_on(today + datetime.timedelta(days=d), hour, minute, zone)
        out.append(Window(start, start + length, zone))
    return out


def current(now, settings):
    """The window `now` is in, or None."""
    for w in windows_around(now, settings):
        if w.start <= now < w.end:
            return w
    return None


def following(now, settings):
    """The next window that starts after `now`."""
    return next(w for w in windows_around(now, settings) if w.start > now)
