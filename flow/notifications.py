"""Durable operational notices, independent of verified flow deliveries."""
import logging,re,os
from datetime import timedelta,datetime,time
from urllib.error import HTTPError
from django.db import transaction
from django.utils import timezone
from .models import NotificationConfig,SystemNotice,ServicePulse,CollectionGap,HistoryImport,BackupRun,WhatsAppConfig,WhatsAppDelivery,PullCursor,Camera
from .services import ZONE

log=logging.getLogger(__name__)

def active_config():
    return NotificationConfig.objects.filter(pk=1,enabled=True).exclude(recipient='').first()

def pulse(name,now=None):
    ServicePulse.objects.update_or_create(name=name,defaults={'checked_at':now or timezone.now()})

def resolve(key,now=None):
    # Notification bookkeeping must never turn a successful import into a failure.
    try:
        with transaction.atomic():
            pending=SystemNotice.objects.filter(key=key,resolved_at__isnull=True)
            if pending.exists():pending.update(resolved_at=now or timezone.now())
    except Exception:log.exception('Não foi possível atualizar a resolução do aviso')

def notice(key,title,reason,now=None):
    """One notification per incident, until explicitly observed healthy again."""
    now=now or timezone.now()
    if not active_config():return
    with transaction.atomic():
        item,created=SystemNotice.objects.get_or_create(key=key,defaults={'title':title[:150],'reason':reason[:700],'first_seen':now,'last_seen':now,'next_attempt':now})
        item=SystemNotice.objects.select_for_update().get(pk=item.pk)
        item.last_seen=now;item.title=title[:150];item.reason=reason[:700]
        if item.resolved_at:
            item.resolved_at=None;item.first_seen=now;item.status='pending';item.attempts=0;item.attempted_at=None;item.accepted_at=None;item.next_attempt=now;item.text='';item.delivery_detail=''
        item.save()
    return item

def safe_notice(*args,**kwargs):
    try:return notice(*args,**kwargs)
    except Exception:log.exception('Não foi possível registrar o aviso operacional')

def runtime_failure(source,error):
    # Never place raw exception strings, URLs, payloads or credentials in WhatsApp.
    safe_notice('runtime:'+source,'Erro no serviço '+source,f'Etapa interrompida por {type(error).__name__}. Consulte os logs do serviço para os detalhes técnicos.')

def blocked_slot(day,slot,reason,now=None):
    safe_notice(f'flow:{day}:{slot.strftime("%H:%M")}','Fluxo não enviado no horário',f'Dia {day.strftime("%d/%m/%Y")}, horário {slot.strftime("%H:%M")} (Brasília). Motivo: {reason}',now=now)

def scan_system(now=None):
    now=now or timezone.now();cfg=active_config()
    if not cfg:return
    since=cfg.activated_at or now
    from .whatsapp import session_info
    try:working=session_info().get('status')=='WORKING'
    except Exception:working=False
    if working:resolve('whatsapp-session',now)
    else:notice('whatsapp-session','WhatsApp indisponível','Sessão desconectada ou gateway indisponível. Avisos ficam registrados e aguardam reconexão; os fluxos dependem da conferência e da janela do horário programado.',now)
    expected={v.strip() for v in os.getenv('PFC_PULL_IPS','').split(',') if v.strip()}
    actual=set(Camera.objects.filter(enabled=True).values_list('ip',flat=True))
    if expected-actual:notice('device-config','Equipamento configurado ausente','Há IPs na coleta configurada sem dispositivo ativo: '+', '.join(sorted(expected-actual)),now)
    else:resolve('device-config',now)
    # Group outstanding gaps by physical device. Replicas do not multiply alerts.
    physical={}
    for camera in Camera.objects.filter(enabled=True).order_by('replica_slot','id'):physical.setdefault(camera.ip,camera)
    for camera in physical.values():
        gaps=CollectionGap.objects.filter(camera__ip=camera.ip).exclude(state='recovered')
        key=f'device:{camera.pk}'
        gap=gaps.order_by('-started_at').first()
        if gap:
            state=PullCursor.objects.filter(camera=camera).first()
            detail=(state.last_error if state and state.last_error else gap.detail or gap.state_label)
            # Protocol errors are generated internally; omit unknown exception bodies.
            detail=detail[:180] if not re.search(r'https?://|sign|token|secret|password',detail,re.I) else 'Falha de consulta; consulte o diagnóstico'
            notice(key,'Falha no contador '+camera.name,f'{camera.name} ({camera.ip}). Motivo: {detail}. Início: {gap.started_at.astimezone(ZONE):%d/%m/%Y %H:%M}. {gaps.count()} interrupção(ões) ainda aguardando reexame completo.',now)
        else:resolve(key,now)
    for job in HistoryImport.objects.filter(status='failed',updated_at__gte=since).select_related('camera'):
        reason=job.error if not re.search(r'https?://|sign|token|secret|password',job.error,re.I) else 'Consulte o diagnóstico'
        notice(f'history:{job.pk}','Falha no reexame histórico',f'{job.camera.name}, importação #{job.pk}. Motivo: {reason or "Importação não concluída"}. O envio do dia pode permanecer bloqueado.',now)
    for item in SystemNotice.objects.filter(key__startswith='history:',resolved_at__isnull=True):
        if HistoryImport.objects.filter(pk=item.key.split(':')[1],status__in=['completed','archived']).exists():resolve(item.key,now)
    for backup in BackupRun.objects.filter(status='failed',finished_at__gte=since):
        notice(f'backup:{backup.pk}','Falha no backup verificado',f'Backup #{backup.pk} não foi concluído ou validado. Consulte Backups no GesTi para o motivo técnico.',now)
    # Both a never-started worker and a worker that stopped are detectable.
    for name,minutes in [('collector',5),('history',5),('closer',10)]:
        beat=ServicePulse.objects.filter(name=name).first();anchor=beat.checked_at if beat else since
        if now-anchor>timedelta(minutes=minutes):notice('worker:'+name,'Serviço sem atividade',f'O serviço {name} está há mais de {minutes} minutos sem confirmar atividade. Pode impedir coleta, recuperação ou envio do fluxo.',now)
        else:resolve('worker:'+name,now)
    from .whatsapp import daily_times,recipients
    flowcfg=WhatsAppConfig.objects.filter(pk=1,enabled=True).first()
    if not flowcfg:return
    local=now.astimezone(ZONE)
    for day in (local.date()-timedelta(days=1),local.date()):
        for value in daily_times(flowcfg,day):
            slot=time.fromisoformat(value);due=datetime.combine(day,slot,ZONE)
            if due<since or now<due+timedelta(minutes=2):continue
            chats=recipients(flowcfg)
            deliveries=WhatsAppDelivery.objects.filter(day=day,scheduled_time=slot,chat_id__in=chats)
            accepted=deliveries.filter(status='accepted').count()
            if chats and accepted==len(chats):resolve(f'flow:{day}:{value}',now);continue
            reasons=[]
            for delivery in deliveries.exclude(status='accepted'):
                if delivery.detail and delivery.detail not in reasons:reasons.append(delivery.detail)
            if not chats:reasons=['Nenhum destinatário configurado.']
            if not reasons:reasons=['Nenhuma confirmação de envio pelo agendador. Verifique atividade do serviço e conexão do WhatsApp.']
            blocked_slot(day,slot,f'{accepted}/{len(chats)} destinatários com aceite do gateway. '+ ' / '.join(reasons),now)

def dispatch_notices(now=None):
    now=now or timezone.now();cfg=active_config()
    if not cfg:return
    # A crashed sender is uncertain; never automatically duplicate its POST.
    SystemNotice.objects.filter(status='sending',attempted_at__lt=now-timedelta(minutes=2)).update(status='unknown',delivery_detail='Processo interrompido durante o envio; resultado incerto. Confira no destinatário.')
    due=SystemNotice.objects.filter(status='pending',next_attempt__lte=now).order_by('first_seen','pk')
    if not due.exists():return
    from .whatsapp import session_info,gateway
    try:working=session_info().get('status')=='WORKING'
    except Exception:working=False
    if not working:
        due.update(next_attempt=now+timedelta(seconds=60),delivery_detail='WhatsApp indisponível; aviso aguardando reconexão.')
        return
    for ident in list(due.values_list('pk',flat=True)[:3]):
        with transaction.atomic():
            locked=NotificationConfig.objects.select_for_update().get(pk=1)
            if not locked.enabled or not re.fullmatch(r'[1-9][0-9]{7,14}',locked.recipient):return
            item=SystemNotice.objects.select_for_update().get(pk=ident)
            if item.status!='pending' or item.next_attempt>now:continue
            # Resolved incidents still notify what happened, with explicit current state.
            text=f'*GesTi · ALERTA AUTOMÁTICO*\n\n{item.title}\n{item.reason}\nDetectado em: {item.first_seen.astimezone(ZONE):%d/%m/%Y %H:%M} (Brasília).'
            if item.resolved_at:text+='\nSituação atual: ocorrência resolvida; aviso emitido após a reconexão.'
            item.recipient=locked.recipient+'@c.us';item.text=text;item.status='sending';item.attempted_at=now;item.attempts+=1;item.save()
        try:
            gateway('POST','/api/sendText',{'session':'default','chatId':item.recipient,'text':item.text})
            item.status='accepted';item.accepted_at=timezone.now();item.delivery_detail='Aceito pelo gateway; entrega no celular não confirmada.'
        except HTTPError as exc:
            if exc.code in (401,403,429):
                item.status='pending';item.next_attempt=now+timedelta(minutes=min(60,2**min(item.attempts,6)))
                item.delivery_detail=f'Gateway recusou o aviso (HTTP {exc.code}); nova tentativa programada.'
            else:
                item.status='unknown';item.delivery_detail=f'Gateway respondeu HTTP {exc.code}; resultado não confirmado. Confira no destinatário antes de repetir.'
        except Exception:
            item.status='unknown';item.delivery_detail='Resultado incerto; sem repetição automática para evitar duplicidade. Confira no destinatário.'
        item.save(update_fields=['status','accepted_at','next_attempt','delivery_detail'])

class ErrorNoticeMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        if response.status_code>=500:
            route=getattr(getattr(request,'resolver_match',None),'route',None) or 'rota não identificada'
            safe_notice('web:'+route,'Erro na aplicação web',f'Uma requisição à rota {route} terminou com HTTP {response.status_code}. Consulte os logs da aplicação.')
        return response
