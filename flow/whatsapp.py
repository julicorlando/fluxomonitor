import json,os,re
from datetime import timedelta
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from django.db import transaction
from django.utils import timezone
from .models import WhatsAppConfig,WhatsAppDelivery
from .services import report,ZONE

def gateway(method,path,body=None,binary=False):
    base=os.getenv('WAHA_URL','http://waha:3000').rstrip('/')
    key=os.getenv('WAHA_API_KEY','')
    if not key:raise RuntimeError('Configure WAHA_API_KEY no Coolify')
    req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'X-Api-Key':key,'Content-Type':'application/json','Accept':'image/png' if binary else 'application/json'})
    with urlopen(req,timeout=20) as response:
        raw=response.read(2097152)
        return raw if binary else json.loads(raw)
def session_info():return gateway('GET','/api/sessions/default')
def start_session():
    try:session_info()
    except HTTPError as e:
        if e.code!=404:raise
        return gateway('POST','/api/sessions',{'name':'default','start':True})
    return gateway('POST','/api/sessions/default/start',{})
def list_groups():
    raw=gateway('GET','/api/default/groups')
    # WAHA supports lists or dictionaries keyed by group ID across engines.
    if isinstance(raw,list):items=raw
    elif isinstance(raw,dict):items=[dict(v,id=v.get('id',k)) for k,v in raw.items() if isinstance(v,dict)]
    else:items=[]
    result=[]
    for g in items:
        ident=g.get('id')
        if isinstance(ident,dict):ident=ident.get('_serialized')
        if isinstance(ident,str) and ident.endswith('@g.us'):result.append((ident,g.get('subject') or g.get('name') or ident))
    return result

def scheduled_send(now=None):
    now=now or timezone.now();local=now.astimezone(ZONE)
    cfg=WhatsAppConfig.objects.filter(pk=1,enabled=True).first()
    if not cfg or not cfg.chat_id:return
    scheduled=local.replace(hour=cfg.send_at.hour,minute=cfg.send_at.minute,second=0,microsecond=0)
    # Run once within 10 minutes; never send a stale previous-day report.
    if not scheduled<=local<scheduled+timedelta(minutes=10):return
    if WhatsAppDelivery.objects.filter(day=local.date()).exclude(status="skipped").exists():return
    try:
        if session_info().get('status')!='WORKING':return
    except Exception:return
    data=report(local.date())
    text=f"Boa noite, fluxo final {data['totals']['entries']}"
    with transaction.atomic():
        locked=WhatsAppConfig.objects.select_for_update().get(pk=1)
        if not locked.enabled or not locked.chat_id:return
        delivery,created=WhatsAppDelivery.objects.get_or_create(day=local.date(),defaults={'chat_id':locked.chat_id,'text':text,'status':'sending'})
        if not created:
            if delivery.status!='skipped':return
            delivery.chat_id=locked.chat_id;delivery.text=text;delivery.status='sending';delivery.detail='';delivery.gateway_id=''
            delivery.save(update_fields=['chat_id','text','status','detail','gateway_id'])
    try:
        result=gateway('POST','/api/sendText',{'session':'default','chatId':delivery.chat_id,'text':delivery.text})
        ident=result.get('id','')
        if isinstance(ident,dict):ident=ident.get('_serialized','')
        delivery.gateway_id=str(ident)[:150];delivery.status='accepted';delivery.detail='Aceito pelo gateway; entrega no destinatário não confirmada'
    except Exception:
        # A timeout may occur after WhatsApp accepts the message. Never retry blindly.
        delivery.status='unknown';delivery.detail='Resultado incerto; confira no WhatsApp. Sem reenvio automático para evitar duplicidade.'
    delivery.save(update_fields=['status','detail','gateway_id'])

def send_test(cfg,actor):
    from .models import AuditLog
    if not cfg.chat_id:raise ValueError('Salve um número ou grupo antes de testar.')
    if session_info().get('status')!='WORKING':raise ValueError('Conecte o WhatsApp pelo QR antes de testar.')
    today=timezone.now().astimezone(ZONE).date()
    text=f"Boa noite, fluxo final {report(today)['totals']['entries']}"
    audit=AuditLog.objects.create(actor=actor,action='whatsapp_test',object_type='WhatsAppConfig',object_id=str(cfg.pk),changes={'status':'sending','recipient':cfg.chat_id,'text':text})
    try:
        gateway('POST','/api/sendText',{'session':'default','chatId':cfg.chat_id,'text':text})
    except Exception:
        audit.changes={**audit.changes,'status':'unknown'};audit.save(update_fields=['changes'])
        raise ValueError('Resultado do teste incerto. Confira no WhatsApp antes de repetir.')
    audit.changes={**audit.changes,'status':'accepted'};audit.save(update_fields=['changes'])
    return text
