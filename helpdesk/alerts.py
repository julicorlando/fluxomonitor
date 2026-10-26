"""Persistent SLA notices and weekly summaries; no ambiguous automatic resend."""
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
import socket
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from .calendar import ZONE, is_open
from .models import CompanySlaConfig, SlaAgentProfile, SlaAlert, Ticket, TicketUpdate, Satisfaction, BotConfig
from .services import company_ids
from .sla import ticket_sla, duration

ACTIVE = ('open','working','waiting')
PHASES = {'response':'primeira resposta','resolution':'resolução'}


def recipient_for(config,ticket,kind):
    user = config.manager if kind in ('escalation','weekly') else ticket.assigned_to
    if not user or config.company_id not in company_ids(user):
        return user,None,'Defina um responsável/gestor ativo com acesso à empresa.'
    profile = SlaAgentProfile.objects.filter(user=user,contact__active=True).select_related('contact').first()
    if not profile:
        return user,None,'Vincule o WhatsApp deste técnico/gestor nas políticas de SLA.'
    return user,profile.contact,''


def valid_event(event,config,ticket,now):
    if event.kind == 'weekly':
        local=now.astimezone(ZONE).date()
        expected_end=local-timedelta(days=local.weekday()+1)
        return config.weekly_enabled and event.period_end==expected_end
    if not config.alerts_enabled or not ticket or ticket.status not in ACTIVE or ticket.sla_cycle != event.cycle or ticket.sla_paused_at:
        return False
    if not is_open(now,ticket.sla_calendar):
        return False
    clock = ticket_sla(ticket,now)[event.phase]
    if clock.get('done') or clock.get('remaining') is None:
        return False
    remaining = clock['remaining']
    if event.kind == 'warning':
        return 0<=remaining<=config.warning_minutes
    if event.kind == 'overdue':
        return remaining<0
    return remaining<=-config.escalation_minutes and remaining<0


def notice_text(config,ticket,phase,kind,clock):
    title = {'warning':'PRAZO PRÓXIMO','overdue':'SLA VENCIDO','escalation':'ESCALONAMENTO AO GESTOR'}[kind]
    due = ticket.response_due_at if phase=='response' else ticket.resolution_due_at
    return (f'*MENSAGEM AUTOMATICA*\n*GesTi · {title}*\nEmpresa: {config.company.code}\n'
            f'Chamado: {ticket.code}\nPrioridade: {ticket.get_priority_display()}\nEtapa: {PHASES[phase]}\n'
            f'Prazo: {due.astimezone(ZONE):%d/%m/%Y %H:%M} (Brasília)\n{clock["clock"]}\n'
            f'Painel: https://helpdesk.shoppingcarpina.com.br/support/tickets/{ticket.pk}/')


def prepare(event,config,ticket,now):
    user,contact,error = recipient_for(config,ticket,event.kind)
    event.recipient,event.contact = user,contact
    event.phone = contact.phone if contact else ''
    event.status = 'blocked' if error else 'pending'
    event.detail = error
    event.next_attempt_at = now
    if ticket:
        event.text = notice_text(config,ticket,event.phase,event.kind,ticket_sla(ticket,now)[event.phase])
    event.save()


def weekly_payload(config,start,end,now):
    from .reporting import dashboard
    cohort = Ticket.objects.filter(company=config.company,created_at__date__gte=start,created_at__date__lte=end)
    stats = dashboard(cohort,now)
    changes = TicketUpdate.objects.filter(ticket__company=config.company,created_at__date__gte=start,created_at__date__lte=end,
        new_status__in=('resolved','closed')).exclude(old_status__in=('resolved','closed'))
    ratings = Satisfaction.objects.filter(ticket__company=config.company,cancelled=False,answered_at__date__gte=start,
        answered_at__date__lte=end,rating__isnull=False)
    values = list(ratings.values_list('rating',flat=True))
    return {'company':config.company.code,'start':start.isoformat(),'end':end.isoformat(),'generated_at':now.isoformat(),
        'opened':cohort.count(),'resolved':changes.values('ticket_id').distinct().count(),
        'backlog':Ticket.objects.filter(company=config.company,status__in=ACTIVE).count(),
        'response_pct':stats['response_pct'],'response_n':stats['response_n'],
        'resolution_pct':stats['resolution_pct'],'resolution_n':stats['resolution_n'],
        'rating_n':len(values),'rating_avg':round(sum(values)/len(values),1) if values else None,
        'csat':round(sum(v>=4 for v in values)/len(values)*100,1) if values else None,
        'reopened':TicketUpdate.objects.filter(ticket__company=config.company,created_at__date__gte=start,
            created_at__date__lte=end,old_status__in=('resolved','closed','cancelled'),new_status__in=ACTIVE).count(),
        'team':list(changes.values('author').annotate(resolved=Count('ticket_id',distinct=True)).order_by('-resolved','author')[:8])}


def weekly_text(data):
    from datetime import date
    stamp = lambda value: '—' if value is None else str(value)
    generated_label=datetime.fromisoformat(data['generated_at']).astimezone(ZONE).strftime('%d/%m/%Y %H:%M')
    start_label=date.fromisoformat(data['start']).strftime('%d/%m/%Y')
    end_label=date.fromisoformat(data['end']).strftime('%d/%m/%Y')
    team = '\n'.join(f'• {row["author"]}: {row["resolved"]} resolvido(s)' for row in data['team']) or 'Sem resoluções registradas na semana.'
    return (f'*MENSAGEM AUTOMATICA*\n*GesTi · RESUMO SEMANAL · {data["company"]}*\n'
        f'{start_label} a {end_label}\nGerado em {generated_label} (Brasília)\nAbertos na semana: {data["opened"]}\n'
        f'Resolvidos na semana: {data["resolved"]}\nReaberturas: {data["reopened"]}\n'
        f'Em aberto na geração: {data["backlog"]}\n\nSLA dos chamados abertos na semana (posição na geração):\n'
        f'Resposta no prazo: {stamp(data["response_pct"])}% · {data["response_n"]} caso(s) medido(s)\n'
        f'Resolução no prazo: {stamp(data["resolution_pct"])}% · {data["resolution_n"]} caso(s) medido(s)\n'
        f'\nAvaliações respondidas na semana: {data["rating_n"]}\nNota média: {stamp(data["rating_avg"])}/5 · CSAT: {stamp(data["csat"])}%\n'
        f'\nResoluções por técnico (até 8):\n{team}\n\nSem casos medidos, “—” indica ausência de dados.\n'
        'Painel: https://helpdesk.shoppingcarpina.com.br/support/')


def scan_alerts(now=None):
    now = now or timezone.now()
    from django.db.models import F
    local_day=now.astimezone(ZONE).date()
    latest_end=local_day-timedelta(days=local_day.weekday()+1)
    SlaAlert.objects.filter(kind='weekly',status__in=('pending','blocked'),period_end__lt=latest_end).update(
        status='cancelled',detail='Janela semanal encerrada; resumo preservado no histórico.')
    SlaAlert.objects.filter(status__in=('pending','blocked'),ticket__isnull=False).filter(
        ~Q(ticket__status__in=ACTIVE)|~Q(cycle=F('ticket__sla_cycle'))).update(status='cancelled',detail='Chamado finalizado ou ciclo alterado.')
    # Keep lock order contact -> ticket -> alert, consistent with ticket updates and dispatch.
    for ticket_id in Ticket.objects.filter(status__in=ACTIVE,sla_started_at__isnull=False).values_list('pk',flat=True).iterator(chunk_size=200):
        with transaction.atomic():
            snapshot = Ticket.objects.get(pk=ticket_id)
            from .models import Contact
            Contact.objects.select_for_update().get(pk=snapshot.contact_id)
            ticket = Ticket.objects.select_for_update(of=('self',)).select_related('company','assigned_to').get(pk=ticket_id)
            config,_ = CompanySlaConfig.objects.get_or_create(company=ticket.company)
            for phase in PHASES:
                for kind in ('warning','overdue','escalation'):
                    key = f'sla:{ticket.pk}:{ticket.sla_cycle}:{phase}:{kind}'
                    candidate = SlaAlert(ticket=ticket,company=ticket.company,cycle=ticket.sla_cycle,phase=phase,kind=kind,key=key)
                    if not valid_event(candidate,config,ticket,now):
                        SlaAlert.objects.filter(key=key,status__in=('pending','blocked')).update(status='cancelled',detail='Condição de SLA não está mais ativa.')
                        continue
                    event,created = SlaAlert.objects.get_or_create(key=key,defaults={'ticket':ticket,'company':ticket.company,
                        'cycle':ticket.sla_cycle,'phase':phase,'kind':kind})
                    event = SlaAlert.objects.select_for_update().get(pk=event.pk)
                    if event.status in ('pending','blocked','cancelled') and (event.attempts==0 or event.status=='blocked'):
                        prepare(event,config,ticket,now)
    for config in CompanySlaConfig.objects.filter(weekly_enabled=True).select_related('company','manager'):
        local = now.astimezone(ZONE)
        week = local.date()-timedelta(days=local.weekday())
        slot = datetime.combine(week+timedelta(days=config.weekly_day),config.weekly_time,ZONE)
        if local < slot:
            continue
        start,end = week-timedelta(days=7),week-timedelta(days=1)
        with transaction.atomic():
            config = CompanySlaConfig.objects.select_for_update(of=('self',)).select_related('company','manager').get(pk=config.pk)
            if not config.weekly_enabled:
                continue
            event,created = SlaAlert.objects.get_or_create(key=f'weekly:{config.company_id}:{start}',defaults={
                'company':config.company,'phase':'weekly','kind':'weekly','period_start':start,'period_end':end})
            event = SlaAlert.objects.select_for_update().get(pk=event.pk)
            if event.status in ('pending','blocked') and (event.attempts==0 or event.status=='blocked'):
                if not event.payload:
                    event.payload = weekly_payload(config,start,end,now)
                    event.text = weekly_text(event.payload)
                prepare(event,config,None,now)


def dispatch_alerts(now=None,limit=5):
    from .transport import verified_destination
    from flow.whatsapp import gateway, session_info
    from .models import Contact
    now = now or timezone.now()
    SlaAlert.objects.filter(status='sending',attempted_at__lt=now-timedelta(minutes=2)).update(
        status='unknown',detail='Processo interrompido durante envio; conferir destinatário, sem repetição automática.')
    if not BotConfig.objects.filter(pk=1,enabled=True).exists():
        return
    ids = list(SlaAlert.objects.filter(status='pending').filter(Q(next_attempt_at__isnull=True)|Q(next_attempt_at__lte=now))
        .order_by('id').values_list('id',flat=True)[:limit])
    if not ids or session_info().get('status') != 'WORKING':
        return
    for pk in ids:
        with transaction.atomic():
            snapshot = SlaAlert.objects.get(pk=pk)
            if snapshot.ticket_id:
                ticket_snapshot = Ticket.objects.get(pk=snapshot.ticket_id)
                Contact.objects.select_for_update().get(pk=ticket_snapshot.contact_id)
                ticket = Ticket.objects.select_for_update(of=('self',)).select_related('company','assigned_to').get(pk=snapshot.ticket_id)
            else:
                ticket = None
            event = SlaAlert.objects.select_for_update().get(pk=pk)
            if event.status != 'pending':
                continue
            config = CompanySlaConfig.objects.select_related('company','manager').get(company_id=event.company_id)
            if not valid_event(event,config,ticket,now):
                event.status,event.detail = 'cancelled','Chamado atualizado, pausa ou agendamento desativado.'
                event.save(update_fields=['status','detail'])
                continue
            user,contact,error = recipient_for(config,ticket,event.kind)
            if error or user != event.recipient or not contact or contact.pk != event.contact_id or contact.phone != event.phone:
                event.status,event.detail = 'blocked',error or 'Destinatário alterado; aguardando revalidação do próximo ciclo.'
                event.save(update_fields=['status','detail'])
                continue
            if ticket:
                event.text = notice_text(config,ticket,event.phase,event.kind,ticket_sla(ticket,now)[event.phase])
            event.status,event.attempted_at = 'sending',now
            event.attempts += 1
            event.save(update_fields=['text','status','attempted_at','attempts'])
        state,detail,external,retry = 'unknown','Resultado incerto; conferir destinatário antes de reenviar.','',False
        try:
            result = gateway('POST','/api/sendText',{'session':'default','chatId':verified_destination(contact),'text':event.text})
            external = result.get('id','') if isinstance(result,dict) else ''
            if isinstance(external,dict):
                external = external.get('_serialized','')
            external = external.strip()[:250] if isinstance(external,str) else ''
            if external:
                state,detail = 'accepted','Aceito pelo gateway; entrega no celular não confirmada.'
            else:
                detail = 'Gateway respondeu sem identificador; sem repetição automática.'
        except HTTPError as exc:
            retry = exc.code==429
            if 400<=exc.code<500 and not retry:
                state,detail = 'failed',f'Envio recusado pelo gateway (HTTP {exc.code}).'
        except URLError as exc:
            retry = isinstance(exc.reason,(ConnectionRefusedError,socket.gaierror))
        except Exception as exc:
            from django.core.exceptions import ValidationError
            if isinstance(exc,ValidationError):
                state,detail = 'blocked','Número não confirmado no WhatsApp; envio não executado.'
        if retry:
            state = 'pending' if event.attempts<5 else 'failed'
            detail = 'Gateway indisponível antes do envio ou limite temporário; nova tentativa segura.'
        SlaAlert.objects.filter(pk=pk,status='sending').update(status=state,detail=detail,gateway_id=external,
            next_attempt_at=now+timedelta(minutes=1) if retry else None)


def sla_pass():
    scan_alerts()
    dispatch_alerts()
