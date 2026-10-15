"""Reversible removal of import-owned data only; live data and cursors stay."""
import json
from datetime import date,timedelta
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Camera,HistoryImport,ImportCleanup,Reading,AuditLog,ScheduledSummary
from .services import close_day,report,ZONE

FIELDS=['id','camera_id','event_id','timestamp','entries','exits','mode','counter_epoch','raw_id']
def imported_readings():
    return Reading.objects.filter(Q(raw__payload__source='pfc-history')|Q(raw__payload__source='pfc-replica',raw__payload__original__source='pfc-history'))
def preview():
    return {'jobs':HistoryImport.objects.exclude(status='archived').count(),'readings':imported_readings().count()}
def recalculate(days):
    for day in days:close_day(date.fromisoformat(day))
    summaries=ScheduledSummary.objects.filter(Q(*[Q(start__lte=d,end__gte=d) for d in days],_connector=Q.OR)) if days else ScheduledSummary.objects.none()
    for summary in summaries:
        if summary.kind=='daily':summary.data=report(summary.start)
        else:
            reports=[report(summary.start+timedelta(days=i)) for i in range((summary.end-summary.start).days+1)]
            summary.data={'days':reports,'passages':sum(d['totals']['passages'] for d in reports)}
        summary.save(update_fields=['data'])

def cleanup(actor):
    with transaction.atomic():
        list(Camera.objects.select_for_update().order_by('ip','replica_slot','id'))
        jobs=list(HistoryImport.objects.select_for_update().exclude(status='archived').values('id','status'))
        rows=list(imported_readings().values(*FIELDS))
        if not jobs and not rows:return None
        days={}
        for row in rows:
            day=row['timestamp'].astimezone(ZONE).date().isoformat()
            days.setdefault(day,[])
            if row['camera_id'] not in days[day]:days[day].append(row['camera_id'])
        data=json.loads(json.dumps({'jobs':jobs,'readings':[{**row,'timestamp':row['timestamp'].isoformat()} for row in rows]},cls=DjangoJSONEncoder))
        archive=ImportCleanup.objects.create(actor=actor,data=data,affected_days=days)
        Reading.objects.filter(pk__in=[r['id'] for r in rows]).delete()
        HistoryImport.objects.filter(pk__in=[j['id'] for j in jobs]).update(status='archived')
        recalculate(days)
        AuditLog.objects.create(actor=actor,action='history_cleanup',object_type='ImportCleanup',object_id=str(archive.pk),changes={'jobs':len(jobs),'readings':len(rows),'days':days})
        return archive

def restore(archive_id,actor):
    with transaction.atomic():
        list(Camera.objects.select_for_update().order_by('ip','replica_slot','id'))
        archive=ImportCleanup.objects.select_for_update().get(pk=archive_id)
        if archive.restored_at:return
        for row in archive.data['readings']:
            values={**row,'timestamp':parse_datetime(row['timestamp'])}
            existing=Reading.objects.filter(camera_id=row['camera_id'],event_id=row['event_id']).first()
            if existing:
                if any(getattr(existing,k)!=values[k] for k in ('timestamp','entries','exits','mode','counter_epoch')):raise ValueError('Evento existente diverge da cópia; restauração cancelada')
            else:Reading.objects.create(**values)
        for job in archive.data['jobs']:
            status='cancelled' if job['status'] in ('pending','running') else job['status']
            HistoryImport.objects.filter(pk=job['id'],status='archived').update(status=status)
        archive.restored_at=timezone.now();archive.save(update_fields=['restored_at'])
        recalculate(archive.affected_days)
        AuditLog.objects.create(actor=actor,action='history_restore',object_type='ImportCleanup',object_id=str(archive.pk),changes={})
