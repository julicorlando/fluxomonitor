"""Recovery follows transport continuity, never the absence of pedestrian events."""
from datetime import timedelta
from django.db import transaction
from django.db.models import Q,Max
from django.utils import timezone
from .models import Camera,CollectionGap,HistoryImport,PullCursor,AuditLog
from .services import ZONE

ACTIVE=('pending','running','paused')
def source_for(camera):
    return Camera.objects.filter(ip=camera.ip,enabled=True).order_by('replica_slot','id').first()

def observe_collection(camera,success,previous_success=None,now=None,*,checkpoint=None):
    now=now or timezone.now()
    source=source_for(camera)
    if not source or source.pk!=camera.pk:return
    resumed=False
    with transaction.atomic():
        Camera.objects.select_for_update().get(pk=source.pk)
        gap=CollectionGap.objects.filter(camera=source,state='open').first()
        threshold=max(180,source.expected_interval*3)
        stale=previous_success and (now-previous_success).total_seconds()>threshold
        if not success or stale:
            if not gap:gap=CollectionGap.objects.create(camera=source,started_at=previous_success or now,checkpoint=checkpoint or {})
        if success and gap:
            gap.ended_at=now;gap.state='waiting';gap.detail='Comunicação restabelecida; aguardando reexame do buffer.'
            gap.save(update_fields=['ended_at','state','detail']);resumed=True
    return resumed

def process_recoveries(now=None):
    now=now or timezone.now();today=now.astimezone(ZONE).date()
    consolidate_recoveries(now)
    for ident in CollectionGap.objects.filter(state__in=['waiting','queued','review']).values_list('id',flat=True):
        with transaction.atomic():
            candidate=CollectionGap.objects.get(pk=ident)
            camera=Camera.objects.select_for_update().get(pk=candidate.camera_id)
            # A nullable job produces an outer join, which PostgreSQL cannot lock.
            # Lock the gap row itself; its optional job is read separately.
            gap=CollectionGap.objects.select_for_update().get(pk=ident)
            if not camera.enabled:continue
            start=gap.started_at.astimezone(ZONE).date();end=(gap.ended_at or now).astimezone(ZONE).date()
            # A manual re-scan can clear a review only if its buffer snapshot was
            # taken after communication returned and it covers the entire gap.
            completed=HistoryImport.objects.filter(camera__ip=camera.ip,status='completed',start__lte=start,end__gte=end,snapshot_at__gte=gap.ended_at or now).filter(Q(recovery_mode='full')|Q(pk=gap.job_id)).first()
            if completed:
                gap.job=completed;gap.state='recovered';gap.detail='Buffer reexaminado após a retomada, com deduplicação.';gap.save();continue
            if gap.state=='review':continue
            if start<today-timedelta(days=89):
                gap.state='review';gap.detail='Falha ultrapassa a retenção de 90 dias; exige revisão manual.';gap.save();continue
            if gap.job_id:
                job=gap.job
                if job.status=='completed':
                    gap.job=None;gap.state='waiting';gap.detail='Reexame anterior à retomada; uma nova fotografia do buffer é necessária.';gap.save()
                    job=None
                if job is None:continue
                if job.status in ACTIVE:continue
                if job.status in ('cancelled','archived'):
                    gap.state='review';gap.detail='Recuperação interrompida pelo operador.';gap.save();continue
                if job.status=='failed':
                    transient=job.error in ('URLError','TimeoutError','ConnectionError','ConnectionResetError','RemoteDisconnected')
                    attempts=CollectionGap.objects.filter(job=job).aggregate(value=Max('attempts'))['value'] or gap.attempts
                    if not transient or attempts>=3:
                        gap.state='review';gap.detail='Recuperação exige revisão: '+job.error[:180];gap.save();CollectionGap.objects.filter(job=job).update(state='review',detail=gap.detail);continue
                    if now<gap.next_retry or HistoryImport.objects.filter(camera__ip=camera.ip,status__in=ACTIVE).exclude(pk=job.pk).exists():continue
                    # Preserve the validated cursor. Never retry conflicting event data.
                    job.status='pending';job.error='';job.save(update_fields=['status','error','updated_at'])
                    gap.attempts=attempts+1;gap.next_retry=now+timedelta(minutes=15*gap.attempts);gap.save();CollectionGap.objects.filter(job=job).update(attempts=gap.attempts,next_retry=gap.next_retry);continue
            state=PullCursor.objects.filter(camera=camera,initialized=True,last_success__gte=now-timedelta(minutes=5),last_error='').first()
            if not state:continue
            existing=HistoryImport.objects.filter(camera__ip=camera.ip,status__in=ACTIVE).order_by('id').first()
            if existing and not (existing.start<=start and existing.end>=end and (existing.target_offset is None or existing.snapshot_at and existing.snapshot_at>=gap.ended_at)):continue
            job=existing or HistoryImport.objects.create(camera=camera,start=start,end=end,automatic=True,recovery_mode='tail' if valid_checkpoint(gap.checkpoint) else 'full',checkpoint=gap.checkpoint if valid_checkpoint(gap.checkpoint) else {})
            gap.job=job;gap.state='queued';gap.attempts=1;gap.next_retry=now+timedelta(minutes=15);gap.save()
            AuditLog.objects.create(actor='system',action='auto_recovery',object_type='HistoryImport',object_id=str(job.pk),changes={'gap':gap.pk,'start':str(start),'end':str(end)})


def valid_checkpoint(value):
    return bool(isinstance(value,dict) and value.get('initialized') and value.get('index_mode') in ('id','offset') and type(value.get('offset')) is int and value['offset']>0 and isinstance(value.get('last_event'),dict) and type(value.get('last_id')) is int and value['last_event'].get('idIndex')==value['last_id'])


def consolidate_recoveries(now):
    """One automatic scan can cover several resumed gaps; manual queues stay intact."""
    ips=CollectionGap.objects.filter(state__in=['waiting','queued']).values_list('camera__ip',flat=True).distinct()
    for ip in ips:
        with transaction.atomic():
            camera=Camera.objects.select_for_update().filter(ip=ip,enabled=True).order_by('replica_slot','id').first()
            if not camera:continue
            if not PullCursor.objects.filter(camera=camera,initialized=True,last_success__gte=now-timedelta(minutes=5),last_error='').exists():continue
            gaps=list(CollectionGap.objects.select_for_update().filter(camera=camera,state__in=['waiting','queued']).order_by('started_at'))
            if len(gaps)<2 or any(not g.ended_at for g in gaps):continue
            start=min(g.started_at for g in gaps).astimezone(ZONE).date()
            end=max(g.ended_at for g in gaps).astimezone(ZONE).date()
            if start<now.astimezone(ZONE).date()-timedelta(days=89):continue
            active=list(HistoryImport.objects.select_for_update().filter(camera__ip=ip,status__in=ACTIVE).order_by('id'))
            if any(j.status=='paused' or not j.automatic for j in active):continue
            if len(active)>1:continue
            cp=gaps[0].checkpoint
            mode='tail' if all(valid_checkpoint(g.checkpoint) and g.checkpoint['index_mode']==cp.get('index_mode') and g.checkpoint['offset']>=cp.get('offset',0) for g in gaps) else 'full'
            job=active[0] if active else None
            if job:
                covered=job.start<=start and job.end>=end and (job.snapshot_at is None or job.snapshot_at>=max(g.ended_at for g in gaps)) and (job.recovery_mode=='full' or mode=='tail' and job.checkpoint.get('index_mode')==cp.get('index_mode') and job.checkpoint.get('offset',10**30)<=cp.get('offset',0))
                if covered:
                    for gap in gaps:
                        if gap.job_id!=job.pk:gap.job=job;gap.state='queued';gap.save(update_fields=['job','state'])
                    continue
                if any(g.job_id and g.job_id!=job.pk for g in gaps):continue
                job.start=min(job.start,start);job.end=max(job.end,end);job.recovery_mode=mode;job.checkpoint=cp if mode=='tail' else {}
                job.initialized=False;job.offset=0;job.first_offset=None;job.target_offset=None;job.snapshot_at=None;job.scanned=0;job.imported=0;job.last_timestamp=None;job.rate_at=None;job.rate_per_second=None;job.status='pending';job.error='';job.end_anchor={};job.save()
            else:
                # Existing cancelled/failed jobs retain their explicit review/retry handling.
                if any(g.job_id for g in gaps):continue
                job=HistoryImport.objects.create(camera=camera,start=start,end=end,automatic=True,recovery_mode=mode,checkpoint=cp if mode=='tail' else {})
            for gap in gaps:
                gap.job=job;gap.state='queued';gap.attempts=max(1,gap.attempts);gap.detail='Interrupções agrupadas em um único reexame automático.';gap.save()
            AuditLog.objects.create(actor='system',action='recovery_grouped',object_type='HistoryImport',object_id=str(job.pk),changes={'gaps':[g.pk for g in gaps],'mode':mode,'counts_preserved':True})
