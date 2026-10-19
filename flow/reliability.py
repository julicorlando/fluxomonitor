"""Recovery follows transport continuity, never the absence of pedestrian events."""
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Camera,CollectionGap,HistoryImport,PullCursor,AuditLog
from .services import ZONE

ACTIVE=('pending','running','paused')
def source_for(camera):
    return Camera.objects.filter(ip=camera.ip,enabled=True).order_by('replica_slot','id').first()

def observe_collection(camera,success,previous_success=None,now=None):
    now=now or timezone.now()
    source=source_for(camera)
    if not source or source.pk!=camera.pk:return
    with transaction.atomic():
        Camera.objects.select_for_update().get(pk=source.pk)
        gap=CollectionGap.objects.filter(camera=source,state='open').first()
        threshold=max(180,source.expected_interval*3)
        stale=previous_success and (now-previous_success).total_seconds()>threshold
        if not success or stale:
            if not gap:gap=CollectionGap.objects.create(camera=source,started_at=previous_success or now)
        if success and gap:
            gap.ended_at=now;gap.state='waiting';gap.detail='Comunicação restabelecida; aguardando reexame do buffer.'
            gap.save(update_fields=['ended_at','state','detail'])

def process_recoveries(now=None):
    now=now or timezone.now();today=now.astimezone(ZONE).date()
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
            completed=HistoryImport.objects.filter(camera__ip=camera.ip,status='completed',start__lte=start,end__gte=end,snapshot_at__gte=gap.ended_at or now).first()
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
                    if not transient or gap.attempts>=3:
                        gap.state='review';gap.detail='Recuperação exige revisão: '+job.error[:180];gap.save();continue
                    if now<gap.next_retry or HistoryImport.objects.filter(camera__ip=camera.ip,status__in=ACTIVE).exclude(pk=job.pk).exists():continue
                    # Preserve the validated cursor. Never retry conflicting event data.
                    job.status='pending';job.error='';job.save(update_fields=['status','error','updated_at'])
                    gap.attempts+=1;gap.next_retry=now+timedelta(minutes=15*gap.attempts);gap.save();continue
            state=PullCursor.objects.filter(camera=camera,initialized=True,last_success__gte=now-timedelta(minutes=5),last_error='').first()
            if not state:continue
            existing=HistoryImport.objects.filter(camera__ip=camera.ip,status__in=ACTIVE).order_by('id').first()
            if existing and not (existing.start<=start and existing.end>=end and (existing.target_offset is None or existing.snapshot_at and existing.snapshot_at>=gap.ended_at)):continue
            job=existing or HistoryImport.objects.create(camera=camera,start=start,end=end)
            gap.job=job;gap.state='queued';gap.attempts=1;gap.next_retry=now+timedelta(minutes=15);gap.save()
            AuditLog.objects.create(actor='system',action='auto_recovery',object_type='HistoryImport',object_id=str(job.pk),changes={'gap':gap.pk,'start':str(start),'end':str(end)})
