"""PFC event collector. Validate offset semantics before importing any counts."""
import hashlib,json,os
from datetime import datetime,time,timedelta,timezone as utc
from urllib.request import Request,urlopen
from django.db import transaction
from django.utils import timezone
from .models import Camera,PullCursor,RawDelivery,Reading,NativeWindow
from .services import ZONE,close_day

PATH='passengerFlow/passengerFlowData/PassengerFlowDataGet'
class ProtocolError(ValueError):
    def __init__(self,message,payload=None):
        super().__init__(message)
        self.payload=payload


def signature(params,secret):
    if not secret:raise ProtocolError('PFC_SERVICE_SECRET obrigatório')
    source=''.join(str(k)+str(params[k]) for k in sorted(params))+secret
    return hashlib.md5(source.encode()).hexdigest().upper()

def fetch(camera,offset,quantity=100,*,timeout=8):
    params={'startIndex':offset,'getQuantity':quantity}
    req=Request(f'http://{camera.ip}:8020/{PATH}',data=json.dumps(params,separators=(',',':')).encode(),method='POST',headers={'Accept':'application/json','Content-Type':'application/json;charset=utf-8','Sign':signature(params,os.environ.get('PFC_SERVICE_SECRET',''))})
    with urlopen(req,timeout=timeout) as response:
        body=response.read(1048577)
    if len(body)>1048576:raise ProtocolError('Resposta acima de 1 MiB')
    data=json.loads(body)
    diagnostic={'source':'pfc-pull','request':params,'response':data}
    if not isinstance(data,dict):raise ProtocolError('Resposta JSON não é objeto',diagnostic)
    result=data.get('result',{})
    if not isinstance(result,dict):raise ProtocolError('result inválido',diagnostic)
    if str(result.get('isError')).lower()!='false' or str(result.get('code'))!='0':
        raise ProtocolError('Equipamento recusou consulta: '+str(result.get('message','resposta inválida'))[:150],diagnostic)
    events=data.get('PassengerFlowData')
    if 'PassengerFlowData' not in data and type(data.get('rptQuantity')) is int and data['rptQuantity']==0:
        events=[]
    if not isinstance(events,list) or len(events)>quantity:raise ProtocolError('PassengerFlowData inválido',diagnostic)
    if data.get('rptQuantity')!=len(events):raise ProtocolError('rptQuantity divergente',diagnostic)
    previous=None
    for e in events:
        if not isinstance(e,dict) or any(type(e.get(k)) is not int or e[k]<0 for k in ('idIndex','timestamp','eventType')):raise ProtocolError('Evento inválido',diagnostic)
        # The device clock can move backwards after synchronization. Pagination
        # follows idIndex, never timestamp; reject ambiguous IDs, not clock changes.
        if previous and e['idIndex']<=previous['idIndex']:raise ProtocolError('IDs de eventos repetidos ou fora de ordem',diagnostic)
        previous=e
    return events

def locate_today(camera,since=None):
    first=fetch(camera,0,2)
    if not first:return 0,'id'
    mode='id'
    if len(first)==2:
        probe=fetch(camera,1,1)
        if probe and probe[0]==first[1]:mode='offset'
        else:
            probe=fetch(camera,first[0]['idIndex']+1,1)
            if not probe or probe[0]!=first[1]:raise ProtocolError('Paginação desconhecida: startIndex não corresponde ao offset nem ao idIndex')
    else:
        probe=fetch(camera,first[0]['idIndex'],1)
        if not probe or probe[0]!=first[0]:raise ProtocolError('Não foi possível validar paginação por idIndex')
    start=int(datetime.combine(since or timezone.now().astimezone(ZONE).date(),time.min,tzinfo=ZONE).timestamp())
    base=first[0]['idIndex'] if mode=='id' else 0
    if first[0]['timestamp']>=start:return base,mode
    low,high=base,base+1
    for _ in range(33):
        page=fetch(camera,high,1)
        if page and mode=='id' and page[0]['idIndex']<high:raise ProtocolError('Firmware retornou evento anterior ao startIndex solicitado')
        if not page or page[0]['timestamp']>=start:break
        low,high=high,base+2*(high-base)
    else:raise ProtocolError('Não foi possível localizar registros atuais')
    while low+1<high:
        mid=(low+high)//2
        page=fetch(camera,mid,1)
        if page and mode=='id' and page[0]['idIndex']<mid:raise ProtocolError('Firmware retornou evento anterior ao startIndex solicitado')
        if not page or page[0]['timestamp']>=start:high=mid
        else:low=mid
    return high,mode

def poll(camera):
    with transaction.atomic():
        camera=Camera.objects.select_for_update().get(pk=camera.pk)
        if NativeWindow.objects.filter(camera=camera).exists() or Reading.objects.filter(camera=camera).exclude(event_id__startswith='pfc-event:').exists():
            raise ProtocolError('Dispositivo já possui outra fonte de contagem; não combinar push e pull')
        state,_=PullCursor.objects.get_or_create(camera=camera)
        if state.initialized and state.last_id is not None and state.offset:
            anchor=fetch(camera,state.last_id if state.index_mode=='id' else state.offset-1,1)
            if not anchor or anchor[0]['idIndex']!=state.last_id or state.last_event and anchor[0]!=state.last_event:state.initialized=False
            elif not state.last_event:state.last_event=anchor[0]
        if not state.initialized:
            state.offset,state.index_mode=locate_today(camera);state.last_id=None;state.last_event=None;state.initialized=True
        events=fetch(camera,state.offset,100)
        if events and state.index_mode=="id" and events[0]["idIndex"]<state.offset:raise ProtocolError("Evento anterior ao cursor; avanço bloqueado")
        now=timezone.now()
        changed_days=set()
        if events:
            raw=RawDelivery.objects.create(camera=camera,payload={'source':'pfc-pull','offset':state.offset,'events':events},status='pull_events')
            for e in events:
                stamp=datetime.fromtimestamp(e['timestamp'],utc.utc)
                if stamp>now+timedelta(minutes=5):raise ProtocolError('Relógio da câmera no futuro; corrija antes de coletar')
                if e['eventType'] in (0,1):
                    defaults={'timestamp':stamp,'entries':int(e['eventType']==0),'exits':int(e['eventType']==1),'mode':'delta','counter_epoch':'','raw':raw}
                    reading,created=Reading.objects.get_or_create(camera=camera,event_id=f"pfc-event:{e['idIndex']}",defaults=defaults)
                    if not created and (reading.timestamp!=stamp or reading.entries!=defaults['entries'] or reading.exits!=defaults['exits']):raise ProtocolError('idIndex reutilizado com evento diferente; coleta bloqueada')
                    if created:changed_days.add(stamp.astimezone(ZONE).date())
                state.last_id=e['idIndex'];state.last_event=e
            state.offset=state.last_id+1 if state.index_mode=='id' else state.offset+len(events)
        state.last_success=now;state.last_error='';state.save()
        camera.last_seen=now;camera.save(update_fields=['last_seen'])
        for day in changed_days:
            if day<now.astimezone(ZONE).date():close_day(day,reason='Recuperação pelo coletor ao vivo')
        return len(events)

def mirror(source,target):
    """Copy confirmed readings; the second .237 registration never polls hardware."""
    with transaction.atomic():
        target=Camera.objects.select_for_update().get(pk=target.pk)
        if source.ip!='192.168.1.237' or target.ip!=source.ip or source.pk==target.pk:raise ProtocolError('Réplica permitida somente para dois cadastros de .237')
        if NativeWindow.objects.filter(camera=target).exists() or Reading.objects.filter(camera=target).exclude(event_id__startswith='pfc-event:').exists():raise ProtocolError('Réplica possui outra fonte de contagem')
        missing=Reading.objects.filter(camera=source).exclude(event_id__in=Reading.objects.filter(camera=target).values('event_id')).select_related('raw')
        raw_map={};days=set();count=0
        for reading in missing.iterator():
            if reading.raw_id not in raw_map:
                raw_map[reading.raw_id]=RawDelivery.objects.create(camera=target,payload={'source':'pfc-replica','source_device_id':source.pk,'original':reading.raw.payload},status='pull_replica')
            Reading.objects.create(camera=target,event_id=reading.event_id,timestamp=reading.timestamp,entries=reading.entries,exits=reading.exits,mode=reading.mode,counter_epoch=reading.counter_epoch,raw=raw_map[reading.raw_id])
            days.add(reading.timestamp.astimezone(ZONE).date());count+=1
        state=PullCursor.objects.get(camera=source)
        PullCursor.objects.update_or_create(camera=target,defaults={'offset':state.offset,'last_id':state.last_id,'index_mode':state.index_mode,'initialized':state.initialized,'last_success':state.last_success,'last_error':'','last_event':state.last_event})
        target.last_seen=source.last_seen;target.save(update_fields=['last_seen'])
        for day in days:
            if day<timezone.now().astimezone(ZONE).date():close_day(day,reason='Sincronização da réplica')
        return count
