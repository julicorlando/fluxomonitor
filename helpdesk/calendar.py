"""Pure calendar arithmetic. All snapshots use the Brasília timezone."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('America/Sao_Paulo')


def windows(day, calendar):
    if day.isoformat() in calendar.get('holidays', []):
        return []
    return [(datetime.combine(day,time.fromisoformat(a),ZONE), datetime.combine(day,time.fromisoformat(b),ZONE))
            for a,b in calendar.get('schedule',{}).get(str(day.weekday()), [])]


def elapsed(start, end, calendar):
    if end <= start:
        return 0.0
    if calendar.get('mode') != 'business':
        return (end-start).total_seconds()/60
    day, last = start.astimezone(ZONE).date(), end.astimezone(ZONE).date()
    total = 0.0
    while day <= last:
        for a,b in windows(day,calendar):
            if min(b,end) > max(a,start):
                total += (min(b,end)-max(a,start)).total_seconds()/60
        day += timedelta(days=1)
    return total


def add_minutes(start, minutes, calendar):
    if calendar.get('mode') != 'business':
        return start+timedelta(minutes=minutes)
    if minutes <= 0:
        return start
    point = start.astimezone(ZONE)
    remaining = float(minutes)
    for _ in range(36600):
        for a,b in windows(point.date(),calendar):
            a = max(a,point)
            available = max(0,(b-a).total_seconds()/60)
            if available and remaining <= available:
                return a+timedelta(minutes=remaining)
            remaining -= available
        point = datetime.combine(point.date()+timedelta(days=1),time.min,ZONE)
    raise ValueError('Expediente sem capacidade para calcular o prazo.')


def is_open(point,calendar):
    if calendar.get('mode') != 'business':
        return True
    local=point.astimezone(ZONE)
    return any(a<=local<b for a,b in windows(local.date(),calendar))
