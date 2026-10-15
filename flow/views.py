import csv,hashlib,hmac,json,os
from datetime import date,timedelta
from functools import wraps
from django.http import JsonResponse,HttpResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Camera,ApiKey,RawDelivery,Reading,DailyReport
from .services import report,close_day,ZONE
from .forms import DeviceForm

def access(scope):
    def deco(fn):
        @wraps(fn)
        def inner(request,*a,**kw):
            request.api_key=None
            if scope=='read' and request.user.is_authenticated:return fn(request,*a,**kw)
            token=kw.pop('device_token',None) or request.headers.get('Authorization','').removeprefix('Bearer ').strip()
            key=ApiKey.objects.filter(digest=hashlib.sha256(token.encode()).hexdigest(),enabled=True).first() if token else None
            if not key or not getattr(key,'can_'+scope):return JsonResponse({'error':'Token inválido ou sem permissão'},status=401)
            request.api_key=key;return fn(request,*a,**kw)
        return inner
    return deco
@login_required
def dashboard(request):return render(request,'dashboard.html')
def health(request):return JsonResponse({'status':'ok'})
@access('read')
def live(request):
    try:day=date.fromisoformat(request.GET.get('date',str(timezone.localdate())))
    except ValueError:return JsonResponse({'error':'Use date=AAAA-MM-DD'},status=400)
    try:
        requested=int(request.GET['device']) if request.GET.get('device') else None
        group=int(request.GET['group']) if request.GET.get('group') else None
    except ValueError:return JsonResponse({'error':'Filtro inválido'},status=400)
    scoped=request.api_key.camera_id if request.api_key else None
    if scoped and requested and scoped!=requested:return JsonResponse({'error':'Sem permissão'},status=403)
    data=report(day,scoped or requested,group,until=timezone.now() if day==timezone.now().astimezone(ZONE).date() else None)
    data['devices']=data['cameras']
    return JsonResponse(data)
@access('read')
def cameras(request):
    qs=Camera.objects.all()
    if request.api_key and request.api_key.camera_id:qs=qs.filter(pk=request.api_key.camera_id)
    return JsonResponse({'cameras':list(qs.values('id','name','ip','serial','enabled','last_seen'))})
@access('read')
def history(request):
    try:
        end=date.fromisoformat(request.GET.get('end',str(timezone.localdate()-timedelta(days=1))))
        start=date.fromisoformat(request.GET.get('start',str(end-timedelta(days=6))))
        if end<start or (end-start).days>366:raise ValueError()
    except ValueError:return JsonResponse({'error':'Período inválido; máximo 367 dias'},status=400)
    days=[]
    while start<=end:
        days.append(report(start,request.api_key.camera_id if request.api_key else None));start+=timedelta(days=1)
    return JsonResponse({'days':days})
@access('read')
def export(request):
    response=live(request)
    if response.status_code!=200:return response
    data=json.loads(response.content)
    out=HttpResponse(content_type='text/csv; charset=utf-8');out['Content-Disposition']='attachment; filename="fluxo.csv"'
    out.write('\ufeff');writer=csv.writer(out);writer.writerow(['Data','Camera','Entradas','Saidas','Saldo','Passagens','Pico 15min'])
    for c in data['cameras']:writer.writerow([data['date'],c['name'],c['entries'],c['exits'],c['balance'],c['passages'],c['peak_15min']])
    return out
@csrf_exempt
@access('ingest')
def ingest(request,heartbeat=False):
    if request.method!='POST':return JsonResponse({'error':'Use POST'},status=405)
    try:
        payload=json.loads(request.body)
        if not isinstance(payload,dict):raise ValueError('JSON deve ser objeto')
        c=request.api_key.camera
        if not c:
            c=Camera.objects.filter(pk=payload.get('device_id',payload.get('camera_id')),enabled=True).first()
        if not c or not c.enabled:raise ValueError('Dispositivo desconhecido')
        if c.ip in os.environ.get('PFC_PULL_IPS','').split(',') and not heartbeat:return JsonResponse({'error':'Dispositivo configurado para coleta pull'},status=409)
        with transaction.atomic():
            c=Camera.objects.select_for_update().get(pk=c.pk)
            c.last_seen=timezone.now();c.save(update_fields=['last_seen'])
            raw=RawDelivery.objects.create(camera=c,payload=payload,status='heartbeat' if heartbeat else 'captured')
            if heartbeat:return JsonResponse({'ok':True})
            required=('event_id','timestamp','entries','exits','mode')
            if not all(k in payload for k in required):return JsonResponse({'ok':True,'status':'captured','detail':'Formato nativo capturado; requer mapeamento para contagem','delivery_id':raw.id},status=202)
            ts=parse_datetime(payload['timestamp'])
            if ts is None or timezone.is_naive(ts) or ts>timezone.now()+timedelta(minutes=5):raise ValueError('timestamp deve incluir fuso e não estar no futuro')
            if payload['mode'] not in ('delta','cumulative'):raise ValueError('mode: delta ou cumulative')
            for k in ('entries','exits'):
                if type(payload[k]) is not int or not 0<=payload[k]<=2147483647:raise ValueError('Contagens devem ser inteiros não negativos')
            eid=payload['event_id'];epoch=payload.get('counter_epoch','')
            if not isinstance(eid,str) or not 1<=len(eid)<=128 or not isinstance(epoch,str) or len(epoch)>80:raise ValueError('event_id ou counter_epoch inválido')
            values=dict(timestamp=ts,entries=payload['entries'],exits=payload['exits'],mode=payload['mode'],counter_epoch=epoch)
            old=Reading.objects.filter(camera=c,event_id=eid).first()
            if old and any(getattr(old,k)!=v for k,v in values.items()):raise ValueError('event_id já existe com conteúdo diferente')
            _,created=Reading.objects.get_or_create(camera=c,event_id=eid,defaults={**values,'raw':raw})
            raw.status='counted' if created else 'duplicate';raw.save(update_fields=['status'])
            day=ts.astimezone(ZONE).date()
            if day<timezone.localdate():close_day(day)
        return JsonResponse({'ok':True,'created':created},status=201 if created else 200)
    except (ValueError,TypeError,KeyError) as e:return JsonResponse({'error':str(e)},status=400)

@login_required
def manage_devices(request,device_id=None):
    if not request.user.is_staff:return HttpResponse('Acesso restrito aos gestores',status=403)
    obj=get_object_or_404(Camera,pk=device_id) if device_id else None
    form=DeviceForm(request.POST or None,instance=obj)
    if request.method=='POST' and form.is_valid():
        form.save();return redirect('/devices/')
    return render(request,'devices.html',{'form':form,'devices':Camera.objects.all(),'editing':obj})
@access('read')
def devices_api(request):
    qs=Camera.objects.all()
    if request.api_key and request.api_key.camera_id:qs=qs.filter(pk=request.api_key.camera_id)
    return JsonResponse({'devices':list(qs.values('id','name','ip','serial','enabled','location','manufacturer','model','last_seen','group_id','include_in_total','expected_interval'))})
@access('read')
def device_api(request,device_id):
    if request.api_key and request.api_key.camera_id and request.api_key.camera_id!=device_id:return JsonResponse({'error':'Sem permissão para este dispositivo'},status=403)
    c=get_object_or_404(Camera,pk=device_id)
    try:day=date.fromisoformat(request.GET.get('date',str(timezone.localdate())))
    except ValueError:return JsonResponse({'error':'Use date=AAAA-MM-DD'},status=400)
    latest=Reading.objects.filter(camera=c).order_by('-timestamp','-id').first()
    data=report(day,c.id)
    from .models import NativeWindow
    native=NativeWindow.objects.filter(camera=c).order_by('-end_time').first()
    return JsonResponse({'native_counter':native.payload if native else None,'device':{'id':c.id,'name':c.name,'ip':c.ip,'serial':c.serial,'location':c.location,'manufacturer':c.manufacturer,'model':c.model,'enabled':c.enabled,'last_seen':c.last_seen},'latest_counter':{'timestamp':latest.timestamp,'entries':latest.entries,'exits':latest.exits,'mode':latest.mode,'counter_epoch':latest.counter_epoch} if latest else None,'day':data})
@access('read')
def device_readings(request,device_id):
    if request.api_key and request.api_key.camera_id and request.api_key.camera_id!=device_id:return JsonResponse({'error':'Sem permissão para este dispositivo'},status=403)
    c=get_object_or_404(Camera,pk=device_id)
    try:
        limit=int(request.GET.get('limit','100'));before=int(request.GET.get('before','0'))
        if not 1<=limit<=1000 or before<0:raise ValueError()
    except ValueError:return JsonResponse({'error':'limit: 1–1000; before: ID positivo'},status=400)
    qs=Reading.objects.filter(camera=c).order_by('-id')
    if before:qs=qs.filter(id__lt=before)
    rows=list(qs.values('id','event_id','timestamp','entries','exits','mode','counter_epoch')[:limit])
    return JsonResponse({'device_id':c.id,'readings':rows,'next_before':rows[-1]['id'] if len(rows)==limit else None})

@access('read')
def groups_api(request):
    from .models import DeviceGroup
    qs=DeviceGroup.objects.all()
    if request.api_key and request.api_key.camera_id:qs=qs.filter(camera=request.api_key.camera)
    return JsonResponse({'groups':list(qs.values('id','name'))})

@access('read')
def demo_api(request):
    from math import sin,pi
    intervals=[{'time':f'{h:02d}:{m:02d}','passages':max(0,int(30*sin((h-7)*pi/14))) if 8<=h<=21 else 0} for h in range(24) for m in (0,15,30,45)]
    cameras=[]
    for i in range(1,4):
        cameras.append({'id':i,'name':f'Demonstração {i}','entries':200*i,'exits':150*i,'balance':50*i,'passages':350*i,'online':True,'last_seen':timezone.now().isoformat(),'peak_15min':'14:00'})
    # Keep chart and headline reconciled for demonstration.
    total=sum(x['passages'] for x in intervals)
    for c in cameras:c['passages']=0
    each=total//3
    for i,c in enumerate(cameras):
        c['passages']=each+(total%3 if i==2 else 0);c['entries']=(c['passages']*3)//5;c['exits']=c['passages']-c['entries'];c['balance']=c['entries']-c['exits']
    return JsonResponse({'demo':True,'date':str(timezone.localdate()),'cameras':cameras,'totals':{k:sum(c[k] for c in cameras) for k in ('entries','exits','balance','passages')},'intervals':intervals,'peak_hour':'14:00','warnings':['DEMONSTRAÇÃO — dados fictícios, sem gravação no banco.'],'updated_at':timezone.now().isoformat()})
