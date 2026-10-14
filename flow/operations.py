from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Camera,OpeningHours,CalendarException,DeviceAlert,ScheduledSummary,Reading
from .services import ZONE,report

def business_window(now):
    local=now.astimezone(ZONE)
    special=CalendarException.objects.filter(day=local.date()).first()
    if special and special.closed:return None
    hours=OpeningHours.objects.filter(weekday=local.weekday()).first()
    if not hours:return local.replace(hour=0,minute=0,second=0,microsecond=0)
    if hours.closed:return None
    t=local.time().replace(tzinfo=None)
    if not hours.opens<=t<=hours.closes:return None
    return local.replace(hour=hours.opens.hour,minute=hours.opens.minute,second=hours.opens.second,microsecond=0)

def monitor_devices(now=None):
    now=now or timezone.now();start=business_window(now)
    for c in Camera.objects.filter(enabled=True):
        with transaction.atomic():
            c=Camera.objects.select_for_update().get(pk=c.pk)
            active=DeviceAlert.objects.filter(camera=c,resolved_at__isnull=True).first()
            fresh=c.last_seen and (now-c.last_seen).total_seconds()<=max(180,c.expected_interval*3)
            if active and fresh:active.resolved_at=now;active.save(update_fields=['resolved_at'])
            if not start or (now-start).total_seconds()<=max(180,c.expected_interval*3):continue
            if not fresh and not active:DeviceAlert.objects.create(camera=c,opened_at=now,reason='Sem contato dentro do intervalo esperado')

def generate_summaries():
    first=Reading.objects.order_by('timestamp').first()
    if not first:return
    today=timezone.localdate();day=first.timestamp.astimezone(ZONE).date()
    while day<today:
        ScheduledSummary.objects.update_or_create(kind='daily',start=day,end=day,defaults={'data':report(day)})
        day+=timedelta(days=1)
    week=first.timestamp.astimezone(ZONE).date()
    week-=timedelta(days=week.weekday())
    while week+timedelta(days=7)<=today:
        days=[report(week+timedelta(days=i)) for i in range(7)]
        ScheduledSummary.objects.update_or_create(kind='weekly',start=week,end=week+timedelta(days=6),defaults={'data':{'days':days,'passages':sum(d['totals']['passages'] for d in days)}})
        week+=timedelta(days=7)
