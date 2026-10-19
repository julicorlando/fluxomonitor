"""Durable operational notices, independent of verified flow deliveries."""
import logging,re,os
from datetime import timedelta,datetime,time
from urllib.error import HTTPError
from django.db import transaction
from django.utils import timezone
from .models import NotificationConfig,SystemNotice,ServicePulse,CollectionGap,HistoryImport,BackupRun,WhatsAppConfig,WhatsAppDelivery,PullCursor,Camera,NoticeBatch
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
            for item in pending:
                item.resolved_at=now or timezone.now()
                if item.status=='accepted':item.recovery_status='pending';item.next_attempt=now or timezone.now()
                item.save(update_fields=['resolved_at','recovery_status','next_attempt'])
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
            item.resolved_at=None;item.first_seen=now;item.status='pending';item.attempts=0;item.attempted_at=None;item.accepted_at=None;item.next_attempt=now;item.text='';item.delivery_detail='';item.recovery_status='';item.recovery_notified_at=None
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
            active=gaps.filter(job__status='running').select_related('job').first()
            if active:
                job=active.job;detail+=f' · Reexame #{job.pk}: {job.progress if job.progress is not None else 0}%'
                if job.eta_seconds is not None:detail+=f', previsão aproximada {max(1,round(job.eta_seconds/60))} min'
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
    SystemNotice.objects.filter(status='sending',attempted_at__lt=now-timedelta(minutes=2)).update(status='unknown',delivery_detail='Processo interrompido durante envio; resultado incerto. Confira no destinatário.')
    SystemNotice.objects.filter(recovery_status='sending',attempted_at__lt=now-timedelta(minutes=2)).update(recovery_status='unknown')
    NoticeBatch.objects.filter(status='sending',created_at__lt=now-timedelta(minutes=2)).update(status='unknown',detail='Processo interrompido; sem reenvio automático.')
    from .whatsapp import session_info
    try:working=session_info().get('status')=='WORKING'
    except Exception:working=False
    if not working:
        SystemNotice.objects.filter(status='pending',next_attempt__lte=now).update(next_attempt=now+timedelta(seconds=60),delivery_detail='WhatsApp indisponível; aviso aguardando reconexão.')
        return
    for phase in ('problem','recovered','digest'):
        dispatch_bundle(phase,now)


def dispatch_bundle(phase,now):
    from .whatsapp import gateway
    with transaction.atomic():
        cfg=NotificationConfig.objects.select_for_update().get(pk=1)
        if not cfg.enabled or not re.fullmatch(r'[1-9][0-9]{7,14}',cfg.recipient):return
        if phase=='problem':qs=SystemNotice.objects.filter(status='pending',next_attempt__lte=now)
        elif phase=='recovered':qs=SystemNotice.objects.filter(recovery_status='pending',resolved_at__isnull=False,next_attempt__lte=now)
        else:
            if cfg.last_digest_at and now-cfg.last_digest_at<timedelta(minutes=max(15,cfg.digest_minutes)):return
            qs=SystemNotice.objects.filter(status='accepted',resolved_at__isnull=True)
        title={'problem':'RESUMO DE ALERTAS','recovered':'SITUAÇÃO NORMALIZADA','digest':'ANDAMENTO DAS PENDÊNCIAS'}[phase]
        text=f'*GesTi · {title}*\n{now.astimezone(ZONE):%d/%m/%Y %H:%M} (Brasília)\n'
        selected=[]
        for item in qs.select_for_update().order_by('first_seen','pk')[:20]:
            part=f'\n• {item.title}\n{item.reason}'
            if item.resolved_at:part+='\nSituação atual: ocorrência resolvida.'
            if len(text)+len(part)>3500:break
            text+=part;selected.append(item)
        if not selected:return
        refs=[{'id':i.pk,'epoch':i.first_seen.isoformat(),'phase':phase,'resolved_in_message':bool(i.resolved_at)} for i in selected]
        batch=NoticeBatch.objects.create(phase=phase,references=refs,recipient=cfg.recipient+'@c.us',text=text,created_at=now)
        if phase in ('problem','digest'):cfg.last_digest_at=now;cfg.save(update_fields=['last_digest_at'])
        for item in selected:
            if phase=='problem':item.status='sending'
            elif phase=='recovered':item.recovery_status='sending'
            item.attempted_at=now;item.attempts+=1;item.recipient=batch.recipient;item.text=text;item.save()
    retry=False
    try:
        gateway('POST','/api/sendText',{'session':'default','chatId':batch.recipient,'text':batch.text})
        batch.status='accepted';batch.accepted_at=timezone.now();batch.detail='Aceito pelo gateway; entrega no celular não confirmada.'
    except HTTPError as error:
        retry=error.code in (401,403,429)
        batch.status='rejected' if retry else 'unknown';batch.detail=f'Gateway respondeu HTTP {error.code}.'
    except Exception:
        batch.status='unknown';batch.detail='Resultado incerto; sem repetição automática para evitar duplicidade. Confira no destinatário.'
    batch.save(update_fields=['status','accepted_at','detail'])
    for ref in refs:
        fields={'delivery_detail':batch.detail}
        state='accepted' if batch.status=='accepted' else 'pending' if retry else 'unknown'
        if phase=='problem':
            fields['status']=state
            if state=='accepted':
                fields['accepted_at']=batch.accepted_at
                if ref.get('resolved_in_message'):fields['recovery_status']='accepted';fields['recovery_notified_at']=batch.accepted_at
        elif phase=='recovered':
            fields['recovery_status']=state
            if state=='accepted':fields['recovery_notified_at']=batch.accepted_at
        if retry:fields['next_attempt']=now+timedelta(minutes=5)
        current=SystemNotice.objects.filter(pk=ref['id'],first_seen=ref['epoch'])
        current.update(**fields)
        if phase=='problem' and state=='accepted' and not ref.get('resolved_in_message'):
            current.filter(resolved_at__isnull=False,recovery_status='').update(recovery_status='pending',next_attempt=now)

class ErrorNoticeMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        response=self.get_response(request)
        if response.status_code>=500 and request.path!='/health/monitor/':
            route=getattr(getattr(request,'resolver_match',None),'route',None) or 'rota não identificada'
            safe_notice('web:'+route,'Erro na aplicação web',f'Uma requisição à rota {route} terminou com HTTP {response.status_code}. Consulte os logs da aplicação.')
        return response
