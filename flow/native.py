import hashlib,json,os
from datetime import datetime,timezone as dtzone,timedelta
from django.http import JsonResponse
from django.db import transaction
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from .models import Camera,RawDelivery,Reading,NativeWindow
from .views import access
from .services import close_day,ZONE

def sign_parameters(params,secret):
    """Documented reference algorithm; validate against firmware sample before use."""
    if not secret:raise ValueError('ServiceSecret real obrigatório')
    if any(type(v) not in (int,str) for v in params.values()):raise ValueError('Parâmetros de assinatura devem ser escalares')
    source='&'.join(f'{k}={params[k]}' for k in sorted(params))
    source=(source+'&' if source else '')+secret
    return hashlib.md5(source.encode('utf-8')).hexdigest().upper()
def ack(sn,code=0,msg='success',status=200):
    return JsonResponse({'code':code,'msg':msg,'data':{'sn':sn,'time':int(timezone.now().timestamp())}},status=status)
def integer(p,k):
    value=p.get(k)
    if type(value) is not int or value<0:raise ValueError(f'{k}: inteiro não negativo obrigatório')
    return value
@csrf_exempt
@access('ingest')
def push(request,heartbeat=False):
    if request.method!='POST':return ack('',2,'Use POST',405)
    sn=''
    try:
        data=json.loads(request.body)
        if not isinstance(data,dict):raise ValueError('JSON deve ser objeto')
        sn=data.get('sn')
        if not isinstance(sn,str) or not 1<=len(sn)<=100:raise ValueError('sn obrigatório')
        c=Camera.objects.filter(serial=sn,enabled=True).first()
        if not c:return ack(sn,1,'SN não cadastrado',404)
        if request.api_key.camera_id!=c.id:return ack(sn,2,'Use token exclusivo deste dispositivo',403)
        if c.ip in os.environ.get('PFC_PULL_IPS','').split(',') and not heartbeat:return ack(sn,2,'Dispositivo configurado para coleta pull',409)
        device_time=integer(data,'time')
        if heartbeat:
            with transaction.atomic():
                c=Camera.objects.select_for_update().get(pk=c.pk)
                c.last_seen=timezone.now();c.save(update_fields=['last_seen'])
                RawDelivery.objects.create(camera=c,payload=data,status='heartbeat')
            return ack(sn)
        for k in ('in','out','passby','turnback','avgStayTime','inAdult','outAdult','passbyAdult','turnbackAdult','inChild','outChild','passbyChild','turnbackChild','total','startTime','endTime'):integer(data,k)
        start,end=data['startTime'],data['endTime']
        if start>end:raise ValueError('startTime maior que endTime')
        timestamp=datetime.fromtimestamp(end,dtzone.utc)
        if timestamp>timezone.now()+timedelta(minutes=5):raise ValueError('Horário do dispositivo no futuro; valide heartbeat e relógio')
        if 'eventList' in data:
            if not isinstance(data['eventList'],list):raise ValueError('eventList deve ser lista')
            if 'len' in data and integer(data,'len')!=len(data['eventList']):raise ValueError('len diverge de eventList')
        with transaction.atomic():
            c=Camera.objects.select_for_update().get(pk=c.pk)
            c.last_seen=timezone.now();c.save(update_fields=['last_seen'])
            raw=RawDelivery.objects.create(camera=c,payload=data,status='captured')
            old=NativeWindow.objects.filter(camera=c,end_time=end).first()
            if old and device_time<old.version_time:
                raw.status='stale';raw.save(update_fields=['status']);return ack(sn)
            if old and device_time==old.version_time:
                if data!=old.payload:raise ValueError('Mesma janela e time com conteúdo divergente; requer validação do firmware')
                raw.status='duplicate';raw.save(update_fields=['status']);return ack(sn)
            if old and start!=old.start_time:raise ValueError('Início da janela mudou; requer reconciliação')
            # Distinct partially overlapping windows cannot safely be added.
            overlap=NativeWindow.objects.filter(camera=c,start_time__lt=end,end_time__gt=start).exclude(end_time=end)
            if overlap.exists():raise ValueError('Janela sobreposta a outra; contagem bloqueada para evitar duplicidade')
            r,_=Reading.objects.update_or_create(camera=c,event_id=f'push-window:{end}',defaults={'timestamp':timestamp,'entries':data['in'],'exits':data['out'],'mode':'delta','counter_epoch':'','raw':raw})
            NativeWindow.objects.update_or_create(camera=c,end_time=end,defaults={'start_time':start,'version_time':device_time,'payload':data,'reading':r})
            raw.status='counted';raw.save(update_fields=['status'])
            day=timestamp.astimezone(ZONE).date()
            if day<timezone.localdate():close_day(day)
        return ack(sn)
    except (ValueError,TypeError,KeyError,OverflowError,OSError) as e:return ack(sn or '',2,str(e),400)
