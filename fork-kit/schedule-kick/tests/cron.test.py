"""schedule-kick's cron lines: the forms it reads, the forms it refuses, and the last slot, checked against a plain
minute-by-minute search. No network, no clock (fixed times only).

  python3 -I -B fork-kit/schedule-kick/tests/cron.test.py

RedCheck breaks each rule in a copy of lib/schedulekick/cron.py and shows its test then fails.
"""
import datetime, os, random, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kick_fixture import UTC, at, cron, red_check  # noqa: E402

THIS = os.path.abspath(__file__)


def brute_last(c, now, days=40):
    t = now.replace(second=0, microsecond=0)
    for _ in range(days * 24 * 60):
        if c.matches(t):
            return t
        t -= datetime.timedelta(minutes=1)
    return None


class Parse(unittest.TestCase):
    def test_the_forms_in_use_are_read(self):
        c = cron.Cron('*/15 2,14 1-5 */3 *')
        self.assertEqual(sorted(c.minutes), [0, 15, 30, 45])
        self.assertEqual(sorted(c.hours), [2, 14])
        self.assertEqual(sorted(c.doms), [1, 2, 3, 4, 5])
        self.assertEqual(sorted(c.months), [1, 4, 7, 10])
        self.assertEqual(sorted(c.dows), list(range(7)))
        self.assertEqual(sorted(cron.Cron('0-30/10 * * * 1-5').minutes), [0, 10, 20, 30])
        self.assertEqual(sorted(cron.Cron('17 4 * * *').minutes), [17])
        self.assertEqual(cron.Cron('  17  4 * *   * ').line, '17 4 * * *')

    def test_other_forms_are_refused_loudly(self):
        for line in ('@daily', '0 0 * * MON', '0 0 * JAN *', '0 0 ? * *', '0 0 L * *', '0 0 * * 1#2', '0 0 15W * *',
                     '0 0 * * * *', '0 0 * *', '', '5/15 * * * *', '60 * * * *', '0 24 * * *', '0 0 0 * *',
                     '0 0 * 13 *', '0 0 * * 7', '*/0 * * * *', '30-10 * * * *', '1,,2 * * * *', '-1 * * * *', None):
            with self.subTest(line=line):
                with self.assertRaises(cron.CronError) as e:
                    cron.Cron(line)
                self.assertTrue(str(e.exception))

    def test_a_step_needs_a_star_or_a_range(self):
        with self.assertRaisesRegex(cron.CronError, 'a step needs'):
            cron.Cron('5/15 * * * *')

    def test_values_out_of_range_are_refused(self):
        for line in ('0 0 * * 7', '0 0 32 * *', '0 0 1-32 * *'):
            with self.subTest(line=line):
                with self.assertRaisesRegex(cron.CronError, 'values must be'):
                    cron.Cron(line)

    def test_exactly_five_fields(self):
        for line in ('0 0 * * * *', '0 0 * * * 2026'):
            with self.subTest(line=line):
                with self.assertRaisesRegex(cron.CronError, 'exactly 5 fields'):
                    cron.Cron(line)


class LastSlot(unittest.TestCase):
    def test_simple_daily(self):
        c = cron.Cron('17 4 * * *')
        self.assertEqual(c.last_slot(at('2026-11-21T12:45:00')), at('2026-11-21T04:17:00'))
        self.assertEqual(c.last_slot(at('2026-11-21T04:17:59')), at('2026-11-21T04:17:00'))
        self.assertEqual(c.last_slot(at('2026-11-21T04:16:59')), at('2026-11-20T04:17:00'))

    def test_today_only_hours_and_minutes_not_yet_past_are_skipped(self):
        c = cron.Cron('50 10,20 * * *')
        self.assertEqual(c.last_slot(at('2026-11-21T20:10:00')), at('2026-11-21T10:50:00'))
        self.assertEqual(c.last_slot(at('2026-11-21T10:49:00')), at('2026-11-20T20:50:00'))
        c = cron.Cron('10,40 9 * * *')
        self.assertEqual(c.last_slot(at('2026-11-21T09:39:00')), at('2026-11-21T09:10:00'))

    def test_another_time_zone_is_read_as_utc(self):
        c = cron.Cron('0 3 * * *')
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        now = datetime.datetime(2026, 11, 21, 2, 0, tzinfo=tz)          # 07:00 UTC
        self.assertEqual(c.last_slot(now), at('2026-11-21T03:00:00'))

    def test_day_of_week_zero_is_sunday(self):
        c = cron.Cron('0 12 * * 0')
        # 2026-11-22 is a Sunday
        self.assertEqual(c.last_slot(at('2026-11-24T00:00:00')), at('2026-11-22T12:00:00'))

    def test_both_day_fields_set_means_either(self):
        c = cron.Cron('0 0 1 * 1')          # the 1st of the month, or any Monday
        # 2026-11-23 is a Monday; 2026-12-01 a Tuesday
        self.assertEqual(c.last_slot(at('2026-11-24T00:00:00')), at('2026-11-23T00:00:00'))
        self.assertEqual(c.last_slot(at('2026-12-01T05:00:00')), at('2026-12-01T00:00:00'))

    def test_a_day_field_with_a_star_step_counts_as_star(self):
        c = cron.Cron('0 0 */2 * 1')        # GitHub: day of month starts with *: both must match
        # 2026-11-23 (Monday, 23rd: odd day, matches */2) and 2026-11-16 (Monday, 16th: no)
        self.assertEqual(c.last_slot(at('2026-11-24T00:00:00')), at('2026-11-23T00:00:00'))
        self.assertEqual(c.last_slot(at('2026-11-22T00:00:00')), at('2026-11-09T00:00:00'))

    def test_a_line_that_never_comes_has_no_slot(self):
        self.assertIsNone(cron.Cron('0 0 31 2 *').last_slot(at('2026-11-21T00:00:00')))
        self.assertEqual(cron.Cron('0 0 29 2 *').last_slot(at('2026-11-21T00:00:00')), at('2024-02-29T00:00:00'))

    def test_matches_a_plain_search_minute_by_minute(self):
        rng = random.Random(748)
        lines = ['*/5 * * * *', '17 4 * * *', '0 */6 * * *', '30 2 * * 1', '0,30 8-18 * * 1-5', '15 3 1,15 * *',
                 '45 23 * * 0,6', '0 0 1 * 1', '*/7 */5 * * *', '59 0 2-9/3 * 3', '10 10 1-10 * *']
        for line in lines:
            c = cron.Cron(line)
            for _ in range(15):
                now = at('2026-01-01T00:00:00') + datetime.timedelta(minutes=rng.randrange(60 * 24 * 600),
                                                                     seconds=rng.randrange(60))
                with self.subTest(line=line, now=now):
                    self.assertEqual(c.last_slot(now), brute_last(c, now))


MUTATIONS = {
    'both day fields: either': ('cron', '        return dom or dow', '        return dom and dow',
                                'LastSlot.test_both_day_fields_set_means_either'),
    'a day field with * counts as *': (
        'cron', "self.dom_star, self.dow_star = parts[2].startswith('*'), parts[4].startswith('*')",
        "self.dom_star, self.dow_star = parts[2] == '*', parts[4] == '*'",
        'LastSlot.test_a_day_field_with_a_star_step_counts_as_star'),
    'cron Sunday is 0': ('cron', '(day.weekday() + 1) % 7 in self.dows', 'day.weekday() in self.dows',
                         'LastSlot.test_day_of_week_zero_is_sunday'),
    'N/step refused': ('cron', '        elif step is not None and b is None:', '        elif False:',
                       'Parse.test_a_step_needs_a_star_or_a_range'),
    'range checked': ('cron', '        if not (lo <= first <= hi and lo <= last <= hi):', '        if False:',
                      'Parse.test_values_out_of_range_are_refused'),
    'five fields': ('cron', '        if len(parts) != 5:', '        if len(parts) < 5:',
                    'Parse.test_exactly_five_fields'),
    'today: later hours skipped': ('cron', '                if back == 0 and hour > now.hour:',
                                   '                if False:', 'LastSlot.test_today_only_hours_and_minutes_not_yet_past_are_skipped'),
    'today: later minutes skipped': (
        'cron', '                    if back == 0 and hour == now.hour and minute > now.minute:',
        '                    if False:', 'LastSlot.test_today_only_hours_and_minutes_not_yet_past_are_skipped'),
    'UTC': ('cron', '        now = now.astimezone(UTC).replace(second=0, microsecond=0)',
            '        now = now.replace(second=0, microsecond=0)', 'LastSlot.test_another_time_zone_is_read_as_utc'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        red_check(self, THIS, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
