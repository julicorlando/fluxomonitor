import json,os,re
from datetime import timedelta,time
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from django.db import transaction
from django.utils import timezone
from .models import WhatsAppConfig,WhatsAppDelivery,CalendarException
from .services import ZONE
from .verification import verified_total,VerificationError
from .adjustments import adjusted_entries

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

def recipients(cfg):
    return list(dict.fromkeys(cfg.chat_ids or ([cfg.chat_id] if cfg.chat_id else [])))

def final_time(day):
    special=day.weekday()==6 or CalendarException.objects.filter(day=day,is_holiday=True).exists()
    return time(21,0) if special else time(22,10)

def daily_times(cfg,day):
    cutoff=final_time(day)
    values=cfg.send_times or [cfg.send_at.strftime('%H:%M')]
    partial=[]
    for value in values:
        try:slot=time.fromisoformat(value)
        except (ValueError,TypeError):continue
        if time(6)<=slot<cutoff:partial.append(slot.strftime('%H:%M'))
    return sorted(set(partial+[cutoff.strftime('%H:%M')]))

def flow_message(local,entries):
    clock=local.time().replace(tzinfo=None)
    if clock>=final_time(local.date()):return f"*MENSAGEM AUTOMATICA*\n\nfluxo final {entries}"
    greeting='Bom dia' if time(6)<=clock<time(12) else ('Boa tarde' if time(12)<=clock<time(18) else 'Boa noite')
    return f"*MENSAGEM AUTOMATICA*\n\n{greeting} fluxo parcial {entries}"

def scheduled_send(now=None):
    now=now or timezone.now();local=now.astimezone(ZONE)
    cfg=WhatsAppConfig.objects.filter(pk=1,enabled=True).first()
    if not cfg or not recipients(cfg):return
    times=daily_times(cfg,local.date())
    # Independent delivery identity for each day + scheduled time.
    for value in sorted(set(times)):
        try:slot=time.fromisoformat(value)
        except (ValueError,TypeError):continue
        scheduled=local.replace(hour=slot.hour,minute=slot.minute,second=0,microsecond=0)
        window_end=scheduled+timedelta(minutes=10) if slot<final_time(local.date()) else local.replace(hour=23,minute=59,second=59,microsecond=999999)
        if not scheduled<=local<window_end:continue
        cutoff=final_time(local.date())
        if slot<cutoff and local.time().replace(tzinfo=None)>=cutoff:continue
        pending=[chat for chat in recipients(cfg) if not WhatsAppDelivery.objects.filter(day=local.date(),scheduled_time=slot,chat_id=chat).exclude(status='skipped').exists()]
        if not pending:continue
        try:
            if session_info().get('status')!='WORKING':continue
        except Exception:continue
        try:entries=verified_total(local.date())
        except Exception as exc:
            reason=str(exc) if isinstance(exc,VerificationError) else 'Não foi possível concluir a verificação dos dados'
            with transaction.atomic():
                locked=WhatsAppConfig.objects.select_for_update().get(pk=1)
                for chat in pending:
                    delivery,_=WhatsAppDelivery.objects.get_or_create(day=local.date(),scheduled_time=slot,chat_id=chat,defaults={'text':'','status':'skipped'})
                    if delivery.status=='skipped':
                        delivery.detail=('Envio bloqueado: '+reason)[:250];delivery.save(update_fields=['detail'])
            continue
        for chat_id in pending:
            with transaction.atomic():
                locked=WhatsAppConfig.objects.select_for_update().get(pk=1)
                current=daily_times(locked,local.date())
                if not locked.enabled or chat_id not in recipients(locked) or value not in current:continue
                adjustment=adjusted_entries(entries,locked.error_margin_percent)
                text=flow_message(local,adjustment['adjusted'])
                delivery,created=WhatsAppDelivery.objects.get_or_create(day=local.date(),scheduled_time=slot,chat_id=chat_id,defaults={'text':text,'status':'sending','adjustment':adjustment})
                if not created:
                    if delivery.status!='skipped':continue
                    delivery.adjustment=adjustment;delivery.chat_id=chat_id;delivery.text=text;delivery.status='sending';delivery.detail='';delivery.gateway_id=''
                    delivery.save(update_fields=['chat_id','text','status','detail','gateway_id','adjustment'])
            try:
                result=gateway('POST','/api/sendText',{'session':'default','chatId':delivery.chat_id,'text':delivery.text})
                ident=result.get('id','')
                if isinstance(ident,dict):ident=ident.get('_serialized','')
                delivery.gateway_id=str(ident)[:150];delivery.status='accepted';delivery.detail='Aceito pelo gateway; entrega no destinatário não confirmada'
            except Exception:
                delivery.status='unknown';delivery.detail='Resultado incerto; confira no WhatsApp. Sem reenvio automático para evitar duplicidade.'
            delivery.save(update_fields=['status','detail','gateway_id'])

def send_test(cfg,actor):
    from .models import AuditLog
    if not recipients(cfg):raise ValueError('Salve um número ou grupo antes de testar.')
    if session_info().get('status')!='WORKING':raise ValueError('Conecte o WhatsApp pelo QR antes de testar.')
    local=timezone.now().astimezone(ZONE)
    try:entries=verified_total(local.date())
    except VerificationError as exc:raise ValueError('Teste bloqueado: '+str(exc)) from exc
    cfg.refresh_from_db()
    if not recipients(cfg):raise ValueError('Salve um número ou grupo antes de testar.')
    adjustment=adjusted_entries(entries,cfg.error_margin_percent)
    text=flow_message(local,adjustment['adjusted'])
    uncertain=0
    for chat_id in recipients(cfg):
        audit=AuditLog.objects.create(actor=actor,action='whatsapp_test',object_type='WhatsAppConfig',object_id=str(cfg.pk),changes={'status':'sending','recipient':chat_id,'text':text,'adjustment':adjustment})
        try:
            gateway('POST','/api/sendText',{'session':'default','chatId':chat_id,'text':text})
            status='accepted'
        except Exception:
            status='unknown';uncertain+=1
        audit.changes={**audit.changes,'status':status};audit.save(update_fields=['changes'])
    if uncertain:raise ValueError(f'{len(recipients(cfg))-uncertain} testes aceitos; {uncertain} com resultado incerto. Confira os destinatários antes de repetir.')
    return text+f' ({len(recipients(cfg))} destinatários)'
