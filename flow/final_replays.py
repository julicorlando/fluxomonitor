"""Explicit replays of immutable sent snapshots, separate from scheduled deliveries."""
import re
import uuid
from datetime import timedelta
from django.core import signing
from django.db import transaction
from django.utils import timezone
from .adjustments import adjusted_entries
from .models import WhatsAppConfig,WhatsAppDelivery,WhatsAppFinalReplay,WhatsAppFinalReplayRecipient,AuditLog
from .notifications import safe_notice,resolve
from .whatsapp import recipients,session_info,gateway

SALT='gesti.final-replay.v1'
FINAL=re.compile(r'fluxo final\s+(\d+)\s*$',re.IGNORECASE)

def recent_finals():
    rows={}
    # Multiple contacts on the same day represent one final, not several days.
    sent=WhatsAppDelivery.objects.filter(status='accepted',text__icontains='fluxo final').order_by('-day','-scheduled_time','-id')
    for delivery in sent.iterator(chunk_size=100):
        match=FINAL.search(delivery.text)
        if not match:continue
        day=delivery.day.isoformat()
        if day not in rows and len(rows)==3:break
        saved=delivery.adjustment
        if not isinstance(saved,dict):raise ValueError(f'{delivery.day:%d/%m/%Y}: envio antigo sem memória do desconto. Reenvio bloqueado para não estimar o total.')
        try:
            original=saved['original']
            expected=adjusted_entries(original,saved['percent'])
        except (ValueError,KeyError,TypeError,ArithmeticError) as error:
            raise ValueError(f'{delivery.day:%d/%m/%Y}: memória de cálculo inválida.') from error
        if any(saved.get(key)!=expected[key] for key in ('adjusted','deducted')) or int(match[1])!=expected['adjusted']:
            raise ValueError(f'{delivery.day:%d/%m/%Y}: texto enviado e memória de cálculo divergentes.')
        adjusted=adjusted_entries(original,10)
        if day in rows:
            if rows[day]['original']!=original:raise ValueError(f'{delivery.day:%d/%m/%Y}: totais diferentes enviados para a mesma data. Confira o histórico antes de reenviar.')
            rows[day]['source_ids'].append(delivery.pk)
        else:rows[day]={'day':day,'date_label':delivery.day.strftime('%d/%m/%Y'),**adjusted,'source_ids':[delivery.pk]}
    return sorted(rows.values(),key=lambda row:row['day'])

def replay_text(rows):
    from datetime import date
    lines=[f"{date.fromisoformat(row['day']):%d/%m/%Y} · Fluxo: {row['adjusted']}" for row in rows]
    return '*MENSAGEM AUTOMATICA*\n\nFluxos finais · desconto de 10% já aplicado\n\n'+'\n'.join(lines)

def preview(actor):
    rows=recent_finals()
    cfg=WhatsAppConfig.objects.filter(pk=1).first()
    chats=recipients(cfg) if cfg else []
    payload={'request_id':str(uuid.uuid4()),'actor':actor,'rows':rows,'recipients':chats}
    return {'rows':rows,'text':replay_text(rows) if rows else '',
            'token':signing.dumps(payload,salt=SALT,compress=True) if rows and chats else '',
            'recipient_count':len(chats)}

def enqueue(token,actor):
    try:
        payload=signing.loads(token,salt=SALT,max_age=900)
        request_id=uuid.UUID(payload['request_id'])
        if payload['actor']!=actor:raise ValueError()
    except (signing.BadSignature,ValueError,TypeError,KeyError) as error:
        raise ValueError('Prévia vencida ou inválida. Atualize a página antes de reenviar.') from error
    with transaction.atomic():
        cfg=WhatsAppConfig.objects.select_for_update().get(pk=1)
        existing=WhatsAppFinalReplay.objects.filter(request_id=request_id).first()
        if existing:return existing,False
        chats=recipients(cfg)
        if not chats:raise ValueError('Salve ao menos um contato ou grupo antes de reenviar.')
        rows=recent_finals()
        if not rows or rows!=payload['rows'] or chats!=payload['recipients']:
            raise ValueError('O histórico ou os destinatários mudaram. Atualize a página e confira a nova prévia.')
        if WhatsAppFinalReplayRecipient.objects.filter(status__in=['pending','sending']).exists():
            raise ValueError('Já há um reenvio na fila. Acompanhe o histórico antes de solicitar outro.')
        if WhatsAppFinalReplay.objects.filter(created_at__gte=timezone.now()-timedelta(seconds=60)).exists():
            raise ValueError('Aguarde um minuto antes de solicitar outro reenvio.')
        replay=WhatsAppFinalReplay.objects.create(request_id=request_id,requested_by=actor,snapshot=rows,text=replay_text(rows))
        WhatsAppFinalReplayRecipient.objects.bulk_create([WhatsAppFinalReplayRecipient(replay=replay,chat_id=chat) for chat in chats])
        AuditLog.objects.create(actor=actor,action='wa_final_replay',object_type='WhatsAppFinalReplay',object_id=str(replay.pk),changes={'days':[r['day'] for r in rows],'percent':'10.00','recipients':len(chats),'status':'queued'})
        return replay,True

def dispatch_final_replays():
    now=timezone.now()
    # A process can die after WhatsApp accepts a request. Never retry an uncertain send.
    stale=WhatsAppFinalReplayRecipient.objects.filter(status='sending',attempted_at__lt=now-timedelta(minutes=2))
    for row in stale:
        changed=WhatsAppFinalReplayRecipient.objects.filter(pk=row.pk,status='sending',attempted_at__lt=now-timedelta(minutes=2)).update(status='unknown',completed_at=now,detail='Processo interrompido; resultado incerto. Confira no destinatário antes de solicitar novo reenvio.')
        if changed:safe_notice(f'final-replay:{row.replay_id}','Reenvio dos fluxos finais com resultado incerto','O processo foi interrompido durante o envio. Confira o destinatário antes de solicitar novo reenvio.')
    expired=WhatsAppFinalReplayRecipient.objects.filter(status='pending',replay__created_at__lt=now-timedelta(minutes=15))
    for row in expired:
        detail='Reenvio expirou após 15 minutos na fila. Confira a conexão e solicite novamente.'
        changed=WhatsAppFinalReplayRecipient.objects.filter(pk=row.pk,status='pending').update(status='blocked',detail=detail,completed_at=now)
        if changed:safe_notice(f'final-replay:{row.replay_id}','Reenvio dos fluxos finais não concluído',detail)
    replay=WhatsAppFinalReplay.objects.filter(deliveries__status='pending').order_by('created_at','pk').first()
    if not replay:return
    key=f'final-replay:{replay.pk}'
    try:working=session_info().get('status')=='WORKING'
    except Exception:working=False
    if not working:
        safe_notice(key,'Reenvio dos fluxos finais aguardando WhatsApp','Sessão desconectada ou gateway indisponível. O reenvio aguarda por até 15 minutos.');return
    for ident in replay.deliveries.filter(status='pending').values_list('pk',flat=True):
        with transaction.atomic():
            cfg=WhatsAppConfig.objects.select_for_update().get(pk=1)
            row=WhatsAppFinalReplayRecipient.objects.select_for_update().get(pk=ident)
            if row.status!='pending':continue
            if row.chat_id not in recipients(cfg) or timezone.now()-replay.created_at>timedelta(minutes=15):
                row.status='blocked';row.detail='Destinatário removido da configuração ou prazo de 15 minutos expirado.';row.completed_at=timezone.now();row.save();continue
            row.status='sending';row.attempted_at=timezone.now();row.save(update_fields=['status','attempted_at'])
        try:
            result=gateway('POST','/api/sendText',{'session':'default','chatId':row.chat_id,'text':replay.text})
            ident=result.get('id','')
            if isinstance(ident,dict):ident=ident.get('_serialized','')
            row.gateway_id=str(ident)[:150];row.status='accepted';row.detail='Aceito pelo gateway; entrega no celular não confirmada.'
        except Exception:
            row.status='unknown';row.detail='Resultado incerto; sem repetição automática. Confira no destinatário antes de solicitar novo reenvio.'
        row.completed_at=timezone.now();row.save(update_fields=['gateway_id','status','detail','completed_at'])
    if replay.deliveries.exclude(status='accepted').exists():
        safe_notice(key,'Reenvio dos fluxos finais não concluído','Um ou mais destinos tiveram envio bloqueado ou resultado incerto. Confira o histórico de reenvios; resultados incertos não são repetidos automaticamente.')
    else:resolve(key)
