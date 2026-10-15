"""Reversible removal of import-owned data only; live data and cursors stay."""
import json
from datetime import date,timedelta
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Camera,HistoryImport,ImportCleanup,Reading,AuditLog,ScheduledSummary
from .services import close_day,report,ZONE,bounds

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

def cleanup(actor,day=None):
    with transaction.atomic():
        list(Camera.objects.select_for_update().order_by('ip','replica_slot','id'))
        jobs_query=HistoryImport.objects.select_for_update().exclude(status='archived')
        readings=imported_readings()
        if day is not None:
            start,end=bounds(day)
            jobs_query=jobs_query.filter(start__lte=day,end__gte=day)
            readings=Reading.objects.filter(timestamp__gte=start,timestamp__lt=end)
        jobs=list(jobs_query.values('id','status'))
        rows=list(readings.values(*FIELDS))
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
        AuditLog.objects.create(actor=actor,action='history_cleanup',object_type='ImportCleanup',object_id=str(archive.pk),changes={'jobs':len(jobs),'readings':len(rows),'days':days,'scope':'today' if day is not None else 'imports'})
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


def reimport_today(actor):
    import os
    day=timezone.now().astimezone(ZONE).date()
    ips={ip.strip() for ip in os.getenv('PFC_PULL_IPS','').split(',') if ip.strip()}
    with transaction.atomic():
        cameras=list(Camera.objects.select_for_update().filter(enabled=True).order_by('ip','replica_slot','id'))
        sources={}
        for camera in cameras:
            if camera.ip in ips:sources.setdefault(camera.ip,camera)
        if not sources:raise ValueError('Nenhum equipamento PFC configurado para reimportação')
        # This action must never erase data from a source we cannot repull.
        start,end=bounds(day)
        if Reading.objects.filter(timestamp__gte=start,timestamp__lt=end).exclude(camera__in=[c for c in cameras if c.ip in ips]).exists():
            raise ValueError('Há dados de hoje em equipamento sem consulta PFC; limpeza bloqueada')
        if Reading.objects.filter(timestamp__gte=start,timestamp__lt=end).exclude(event_id__startswith='pfc-event:',mode='delta').exists():
            raise ValueError('Há dados de outra origem hoje; limpeza automática bloqueada')
        archive=cleanup(actor,day=day)
        jobs=[HistoryImport.objects.create(camera=c,start=day,end=day) for c in sources.values()]
        AuditLog.objects.create(actor=actor,action='history_reimport_today',object_type='ImportCleanup',object_id=str(archive.pk if archive else ''),changes={'day':str(day),'jobs':[j.pk for j in jobs]})
        return archive,jobs
