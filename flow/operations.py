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
    from .models import PullCursor
    from django.db.models import Q
    for c in Camera.objects.filter(enabled=True):
        with transaction.atomic():
            c=Camera.objects.select_for_update().get(pk=c.pk)
            active=DeviceAlert.objects.filter(camera=c,kind='communication',resolved_at__isnull=True).first()
            fresh=c.last_seen and (now-c.last_seen).total_seconds()<=max(180,c.expected_interval*3)
            if active and fresh:active.resolved_at=now;active.save(update_fields=['resolved_at'])
            if start and (now-start).total_seconds()>max(180,c.expected_interval*3) and not fresh and not active:
                DeviceAlert.objects.create(camera=c,kind='communication',opened_at=now,reason='Sem contato dentro do intervalo esperado')
            idle=DeviceAlert.objects.filter(camera=c,kind='idle',resolved_at__isnull=True).first()
            hours=OpeningHours.objects.filter(weekday=now.astimezone(ZONE).weekday(),closed=False).exists()
            cursor=PullCursor.objects.filter(camera=c,initialized=True,last_error='',last_success__gte=now-timedelta(minutes=3)).exists()
            latest=Reading.objects.filter(camera=c,mode='delta',timestamp__lte=now).filter(Q(entries__gt=0)|Q(exits__gt=0)).order_by('-timestamp').first()
            anchor=max(start,latest.timestamp) if start and latest else start
            quiet=bool(c.idle_alert_minutes and start and hours and fresh and cursor and anchor and now-anchor>=timedelta(minutes=c.idle_alert_minutes))
            if idle and not quiet:idle.resolved_at=now;idle.save(update_fields=['resolved_at'])
            if quiet and not idle:
                DeviceAlert.objects.create(camera=c,kind='idle',opened_at=now,reason=f'Sem movimento registrado há pelo menos {c.idle_alert_minutes} min com comunicação ativa. Confira a operação do acesso.')

def generate_summaries():
    from .models import DailyReport
    from .intelligence import weekly_executive
    from .services import close_day
    import hashlib,json
    first=Reading.objects.order_by('timestamp').first()
    if not first:return
    today=timezone.now().astimezone(ZONE).date();day=first.timestamp.astimezone(ZONE).date()
    existing={d.day:d for d in DailyReport.objects.filter(day__gte=day,day__lt=today)}
    while day<today:
        data=existing[day].data if day in existing else close_day(day,reason='Fechamento diário')
        old=ScheduledSummary.objects.filter(kind='daily',start=day,end=day).first()
        if not old or old.data!=data:ScheduledSummary.objects.update_or_create(kind='daily',start=day,end=day,defaults={'data':data})
        day+=timedelta(days=1)
    earliest=first.timestamp.astimezone(ZONE).date();earliest-=timedelta(days=earliest.weekday())
    week=today-timedelta(days=today.weekday()+7);rebuilt=0
    while week>=earliest:
        reports=list(DailyReport.objects.filter(day__gte=week-timedelta(days=7),day__lt=week+timedelta(days=7)).order_by('day').values('day','updated_at'))
        fingerprint=hashlib.sha256(json.dumps(reports,default=str,sort_keys=True).encode()).hexdigest()
        old=ScheduledSummary.objects.filter(kind='weekly',start=week,end=week+timedelta(days=6)).first()
        if not old or old.data.get('fingerprint')!=fingerprint or old.data.get('version')!=3:
            data=weekly_executive(week);data['fingerprint']=fingerprint
            ScheduledSummary.objects.update_or_create(kind='weekly',start=week,end=week+timedelta(days=6),defaults={'data':data})
            rebuilt+=1
            # Backfill old executive summaries gradually; prioritize the latest.
            if rebuilt>=1:break
        week-=timedelta(days=7)
