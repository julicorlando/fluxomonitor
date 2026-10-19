from datetime import datetime,time,timedelta,timezone as utc
from django.db import transaction
from django.utils import timezone
from .models import Camera,HistoryImport,Reading,RawDelivery,NativeWindow
from .pfc_pull import fetch,ProtocolError
from .services import ZONE,close_day
from .reliability import valid_checkpoint

def find_end(camera,base):
    """Find an exclusive index bound without assuming timestamp order."""
    low,high=base,base+1
    for _ in range(63):
        if not fetch(camera,high,1):break
        low,high=high,base+2*(high-base)
    else:raise ProtocolError('Não foi possível estimar o limite do histórico')
    while low+1<high:
        mid=(low+high)//2
        if fetch(camera,mid,1):low=mid
        else:high=mid
    return high

def import_batch(job_id):
    with transaction.atomic():
        camera_id=HistoryImport.objects.values_list('camera_id',flat=True).get(pk=job_id)
        camera=Camera.objects.select_for_update().get(pk=camera_id)
        job=HistoryImport.objects.select_for_update().get(pk=job_id)
        if job.status not in ('pending','running'):return
        if not camera.enabled:raise ProtocolError('Dispositivo desativado')
        if NativeWindow.objects.filter(camera=camera).exists() or Reading.objects.filter(camera=camera).exclude(event_id__startswith='pfc-event:').exists():raise ProtocolError('Outra fonte de contagem existente; não misturar fontes')
        if job.recovery_mode=='tail':
            cp=job.checkpoint
            state=camera.pullcursor if hasattr(camera,'pullcursor') else None
            valid=valid_checkpoint(cp) and state and state.initialized and state.index_mode==cp['index_mode'] and state.offset>=cp['offset']
            anchor=fetch(camera,cp['last_id'] if cp.get('index_mode')=='id' else cp.get('offset',1)-1,1) if valid else []
            if not valid or not anchor or anchor[0]!=cp['last_event']:
                fallback_full(job,'Ponto anterior ausente ou alterado; varredura completa necessária.');return
            if not job.initialized:
                job.index_mode=cp['index_mode'];job.offset=cp['offset'];job.first_offset=job.offset
                first=fetch(camera,job.offset,1)
                job.target_offset=find_end(camera,job.offset) if first else job.offset
                end_page=fetch(camera,job.target_offset-1,1) if first else [cp['last_event']]
                if not end_page:fallback_full(job,'Limite final não confirmado; varredura completa necessária.');return
                job.end_anchor=end_page[0]
                job.snapshot_at=timezone.now();job.initialized=True
        if not job.initialized:
            # Determine pagination without searching by timestamps: clocks in
            # retained records are not guaranteed to be monotonic.
            first=fetch(camera,0,2)
            job.offset=0;job.index_mode='id'
            if first:
                job.earliest_available=datetime.fromtimestamp(first[0]['timestamp'],utc.utc)
                job.offset=first[0]['idIndex']
                if len(first)==2:
                    probe=fetch(camera,1,1)
                    if probe and probe[0]==first[1]:
                        job.offset=0;job.index_mode='offset'
                    else:
                        probe=fetch(camera,first[0]['idIndex']+1,1)
                        if not probe or probe[0]!=first[1]:raise ProtocolError('Paginação histórica desconhecida')
                else:
                    probe=fetch(camera,first[0]['idIndex'],1)
                    if not probe or probe[0]!=first[0]:raise ProtocolError('Paginação histórica desconhecida')
            job.initialized=True
        if job.target_offset is None:
            job.first_offset=job.offset
            job.target_offset=find_end(camera,job.offset)
            job.snapshot_at=timezone.now()
        events=fetch(camera,job.offset,100)
        if job.index_mode=='id':events=[e for e in events if e['idIndex']<job.target_offset]
        else:events=events[:max(0,job.target_offset-job.offset)]
        start=datetime.combine(job.start,time.min,tzinfo=ZONE)
        end=datetime.combine(job.end+timedelta(days=1),time.min,tzinfo=ZONE)
        if job.recovery_mode=='tail' and not events and job.offset<job.target_offset:
            fallback_full(job,'Trecho indisponível antes do limite esperado; varredura completa necessária.');return
        if events and job.index_mode=='id' and events[0]['idIndex']<job.offset:raise ProtocolError('Resposta anterior ao cursor histórico')
        raw=None;days=set();job.status='running'
        for event in events:
            stamp=datetime.fromtimestamp(event['timestamp'],utc.utc)
            job.scanned+=1;job.last_timestamp=stamp
            # Continue past records outside the range: a later ID can carry an
            # earlier timestamp after a device clock adjustment.
            if job.recovery_mode=='tail' or start<=stamp<end:
                if stamp>timezone.now()+timedelta(minutes=5):raise ProtocolError('Relógio da câmera no futuro')
                if raw is None:raw=RawDelivery.objects.create(camera=camera,payload={'source':'pfc-history','job_id':job.pk,'events':events},status='pull_history')
                if event['eventType'] in (0,1):
                    defaults={'timestamp':stamp,'entries':int(event['eventType']==0),'exits':int(event['eventType']==1),'mode':'delta','counter_epoch':'','raw':raw}
                    reading,created=Reading.objects.get_or_create(camera=camera,event_id=f"pfc-event:{event['idIndex']}",defaults=defaults)
                    if not created and (reading.timestamp!=stamp or reading.entries!=defaults['entries'] or reading.exits!=defaults['exits']):raise ProtocolError('idIndex reutilizado com conteúdo diferente')
                    if created:job.imported+=1;days.add(stamp.astimezone(ZONE).date())
        if events:
            job.offset=events[-1]['idIndex']+1 if job.index_mode=='id' else job.offset+len(events)
        else:job.status='completed'
        if job.offset>=job.target_offset:job.status='completed'
        if job.status=='completed' and job.recovery_mode=='tail':
            cp=job.checkpoint
            left=fetch(camera,cp['last_id'] if job.index_mode=='id' else cp['offset']-1,1)
            right=fetch(camera,job.target_offset-1,1) if job.target_offset>job.first_offset else left
            if not left or left[0]!=cp['last_event'] or not right or right[0]!=job.end_anchor:
                # The inserted events are retained and must also refresh past-day summaries.
                for day in days:
                    if day<timezone.now().astimezone(ZONE).date():close_day(day,reason=f'Importação histórica #{job.pk}; validação pendente')
                fallback_full(job,'Validação final das âncoras falhou; varredura completa necessária.');return
        now=timezone.now()
        if job.rate_at and (now-job.rate_at).total_seconds()>=5:
            speed=max(0,(job.offset-job.rate_offset)/(now-job.rate_at).total_seconds())
            job.rate_per_second=speed if not job.rate_per_second else 0.3*speed+0.7*job.rate_per_second
            job.rate_at=now;job.rate_offset=job.offset
        elif job.rate_at is None:job.rate_at=now;job.rate_offset=job.offset
        job.error='';job.save()
        for day in days:
            if day<timezone.now().astimezone(ZONE).date():close_day(day,reason=f'Importação histórica #{job.pk}')


def fallback_full(job,reason):
    # Previously inserted events stay deduplicated; no stored counts are removed.
    job.recovery_mode='full';job.checkpoint={};job.end_anchor={};job.fallback_reason=reason
    job.initialized=False;job.first_offset=None;job.target_offset=None;job.snapshot_at=None;job.offset=0;job.scanned=0;job.imported=0;job.last_timestamp=None;job.rate_at=None;job.rate_per_second=None;job.status='pending';job.error='';job.save()
