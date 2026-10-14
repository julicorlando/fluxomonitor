from datetime import datetime,time,timedelta,timezone as utc
from django.db import transaction
from django.utils import timezone
from .models import Camera,HistoryImport,Reading,RawDelivery,NativeWindow
from .pfc_pull import fetch,locate_today,ProtocolError
from .services import ZONE,close_day

def import_batch(job_id):
    with transaction.atomic():
        job=HistoryImport.objects.select_for_update().get(pk=job_id)
        if job.status not in ('pending','running'):return
        camera=Camera.objects.select_for_update().get(pk=job.camera_id)
        if not camera.enabled:raise ProtocolError('Dispositivo desativado')
        if NativeWindow.objects.filter(camera=camera).exists() or Reading.objects.filter(camera=camera).exclude(event_id__startswith='pfc-event:').exists():raise ProtocolError('Outra fonte de contagem existente; não misturar fontes')
        if not job.initialized:
            first=fetch(camera,0,1)
            if first:job.earliest_available=datetime.fromtimestamp(first[0]['timestamp'],utc.utc)
            job.offset,job.index_mode=locate_today(camera,job.start)
            job.initialized=True
        events=fetch(camera,job.offset,100)
        start=datetime.combine(job.start,time.min,tzinfo=ZONE)
        end=datetime.combine(job.end+timedelta(days=1),time.min,tzinfo=ZONE)
        if events and job.index_mode=='id' and events[0]['idIndex']<job.offset:raise ProtocolError('Resposta anterior ao cursor histórico')
        raw=None;days=set();job.status='running'
        for event in events:
            stamp=datetime.fromtimestamp(event['timestamp'],utc.utc)
            if stamp>timezone.now()+timedelta(minutes=5):raise ProtocolError('Relógio da câmera no futuro')
            if stamp>=end:job.status='completed';break
            job.scanned+=1;job.last_timestamp=stamp
            if stamp>=start:
                if raw is None:raw=RawDelivery.objects.create(camera=camera,payload={'source':'pfc-history','job_id':job.pk,'events':events},status='pull_history')
                if event['eventType'] in (0,1):
                    defaults={'timestamp':stamp,'entries':int(event['eventType']==0),'exits':int(event['eventType']==1),'mode':'delta','counter_epoch':'','raw':raw}
                    reading,created=Reading.objects.get_or_create(camera=camera,event_id=f"pfc-event:{event['idIndex']}",defaults=defaults)
                    if not created and (reading.timestamp!=stamp or reading.entries!=defaults['entries'] or reading.exits!=defaults['exits']):raise ProtocolError('idIndex reutilizado com conteúdo diferente')
                    if created:job.imported+=1;days.add(stamp.astimezone(ZONE).date())
        if events:
            job.offset=events[-1]['idIndex']+1 if job.index_mode=='id' else job.offset+len(events)
        else:job.status='completed'
        job.error='';job.save()
        for day in days:
            if day<timezone.now().astimezone(ZONE).date():close_day(day)
