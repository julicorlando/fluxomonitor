import csv,hashlib,hmac,json
from datetime import date,timedelta
from functools import wraps
from django.http import JsonResponse,HttpResponse
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .models import Camera,ApiKey,RawDelivery,Reading,DailyReport
from .services import report,close_day,ZONE

def access(scope):
    def deco(fn):
        @wraps(fn)
        def inner(request,*a,**kw):
            request.api_key=None
            if scope=='read' and request.user.is_authenticated:return fn(request,*a,**kw)
            token=request.headers.get('Authorization','').removeprefix('Bearer ').strip()
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
    return JsonResponse(report(day))
@access('read')
def cameras(request):return JsonResponse({'cameras':list(Camera.objects.values('id','name','ip','serial','enabled','last_seen'))})
@access('read')
def history(request):
    try:
        end=date.fromisoformat(request.GET.get('end',str(timezone.localdate()-timedelta(days=1))))
        start=date.fromisoformat(request.GET.get('start',str(end-timedelta(days=6))))
        if end<start or (end-start).days>366:raise ValueError()
    except ValueError:return JsonResponse({'error':'Período inválido; máximo 367 dias'},status=400)
    days=[]
    while start<=end:
        days.append(report(start));start+=timedelta(days=1)
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
            c=Camera.objects.filter(pk=payload.get('camera_id'),enabled=True).first()
        if not c or not c.enabled:raise ValueError('Câmera desconhecida')
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
