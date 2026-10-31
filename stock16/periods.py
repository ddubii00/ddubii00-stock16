"""One period selector shared by fund changes and Consensus."""
import calendar
from datetime import date, timedelta

PERIODS = {'1d': 1, '1w': 7, '1m': 1, '3m': 3, 'quarter': 3, 'half': 6, '1y': 12}
SUPPORTED_PERIODS = ('previous', '1d', '1w', '5b', '20b', '1m', '3m', 'quarter', 'half', '1y')


def calendar_cutoff(day, period):
    if period in ('1d', '1w'):
        return day - timedelta(days=PERIODS[period])
    months = PERIODS[period]
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    is_month_end = day.day == calendar.monthrange(day.year, day.month)[1]
    return date(year, month, last if is_month_end else min(day.day, last))


def select_pair(connection, fund, period='previous', asof=None, from_date=None):
    if period not in SUPPORTED_PERIODS:
        raise ValueError('Unsupported comparison period')
    after = connection.execute('''SELECT * FROM effective_snapshots
      WHERE fund_id=? AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1''',
      (fund, asof or '9999-12-31')).fetchone()
    if after is None:
        return None, None
    if from_date:
        query = 'as_of_date<=?'
        args = (from_date,)
    elif period == 'previous':
        query = 'as_of_date<?'
        args = (after['as_of_date'],)
    elif period in ('5b', '20b'):
        before = connection.execute('''SELECT * FROM effective_snapshots WHERE fund_id=?
          AND as_of_date<? ORDER BY as_of_date DESC LIMIT 1 OFFSET ?''',
          (fund, after['as_of_date'], int(period[:-1]) - 1)).fetchone()
        return (dict(before) if before else None), dict(after)
    else:
        query = 'as_of_date<=?'
        args = (calendar_cutoff(date.fromisoformat(after['as_of_date']), period).isoformat(),)
    before = connection.execute('SELECT * FROM effective_snapshots WHERE fund_id=? AND '+query+
      ' AND as_of_date<? ORDER BY as_of_date DESC LIMIT 1',
      (fund, *args, after['as_of_date'])).fetchone()
    return (dict(before) if before else None), dict(after)
