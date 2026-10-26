"""Versioned calendars, auditable pauses and SLA clocks for each ticket cycle."""
from datetime import datetime
from django.utils import timezone
from .models import SlaPolicy, CompanySlaConfig
from .calendar import add_minutes, elapsed


def start_sla(ticket, now=None):
    now = now or timezone.now()
    policy = SlaPolicy.objects.get(priority=ticket.priority)
    config,_ = CompanySlaConfig.objects.get_or_create(company_id=ticket.company_id)
    ticket.sla_calendar = {'mode':config.mode, 'schedule':config.schedule, 'holidays':config.holidays,
        'pause_waiting':config.pause_waiting, 'response_minutes':policy.response_minutes,
        'resolution_minutes':policy.resolution_minutes}
    ticket.sla_started_at = now
    ticket.sla_pauses, ticket.sla_paused_at = [], None
    ticket.response_due_at = add_minutes(now,policy.response_minutes,ticket.sla_calendar)
    ticket.resolution_due_at = add_minutes(now,policy.resolution_minutes,ticket.sla_calendar)
    ticket.first_responded_at = None


def used_minutes(ticket, point):
    calendar = ticket.sla_calendar
    used = elapsed(ticket.sla_started_at,point,calendar)
    for pause in ticket.sla_pauses:
        a,b = datetime.fromisoformat(pause[0]),datetime.fromisoformat(pause[1])
        used -= elapsed(max(ticket.sla_started_at,a), min(point,b), calendar)
    if ticket.sla_paused_at and point > ticket.sla_paused_at:
        used -= elapsed(ticket.sla_paused_at,point,calendar)
    return max(0,used)


def finish_pause(ticket, now):
    if not ticket.sla_paused_at:
        return
    minutes = elapsed(ticket.sla_paused_at,now,ticket.sla_calendar)
    if not ticket.first_responded_at:
        ticket.response_due_at = add_minutes(ticket.response_due_at,minutes,ticket.sla_calendar)
    ticket.resolution_due_at = add_minutes(ticket.resolution_due_at,minutes,ticket.sla_calendar)
    ticket.sla_pauses = ticket.sla_pauses+[[ticket.sla_paused_at.isoformat(),now.isoformat()]]
    ticket.sla_paused_at = None


def waiting_transition(ticket, previous, now):
    if not ticket.sla_started_at or not ticket.sla_calendar.get('pause_waiting'):
        return
    if ticket.status == 'waiting' and not ticket.sla_paused_at:
        ticket.sla_paused_at = now
    elif ticket.status != 'waiting':
        finish_pause(ticket,now)


def reprioritize(ticket, now=None):
    now = now or timezone.now()
    finish_pause(ticket,now)
    policy = SlaPolicy.objects.get(priority=ticket.priority)
    ticket.sla_calendar = {**ticket.sla_calendar,'response_minutes':policy.response_minutes,'resolution_minutes':policy.resolution_minutes}
    # Pauses shift each deadline by the useful time excluded from its clock.
    for name,minutes in [('response_due_at',policy.response_minutes),('resolution_due_at',policy.resolution_minutes)]:
        point = ticket.first_responded_at if name == 'response_due_at' else None
        paused = sum(elapsed(datetime.fromisoformat(a), min(datetime.fromisoformat(b),point or now),ticket.sla_calendar)
                     for a,b in ticket.sla_pauses if datetime.fromisoformat(a) < (point or now))
        setattr(ticket,name,add_minutes(ticket.sla_started_at, minutes+paused,ticket.sla_calendar))
    if ticket.status == 'waiting' and ticket.sla_calendar.get('pause_waiting'):
        ticket.sla_paused_at = now


def duration(seconds):
    if seconds is None:
        return '—'
    minutes = max(0,int(seconds//60))
    if minutes < 60:
        return f'{minutes} min'
    hours,minutes = divmod(minutes,60)
    days,hours = divmod(hours,24)
    return f'{days}d {hours}h' if days else f'{hours}h {minutes:02d}min'


def phase(ticket, name, now):
    start = ticket.sla_started_at
    due = ticket.response_due_at if name == 'response' else ticket.resolution_due_at
    done = ticket.first_responded_at if name == 'response' else ticket.resolved_at
    if not start or not due:
        return {'state':'legacy','label':'Não iniciado','progress':0,'clock':'Sem prazo anterior','remaining':None}
    calendar = ticket.sla_calendar
    target = calendar.get(name+'_minutes')
    if target is None:
        target = elapsed(start,due,calendar)
    used = used_minutes(ticket,done or now)
    remaining = target-used
    breached = remaining < -1e-7
    paused = bool(ticket.sla_paused_at and not done)
    risk = not done and not breached and remaining <= min(60,target*.2)
    state = 'breached' if breached else 'paused' if paused else 'risk' if risk else 'ok'
    label = ('Fora do prazo' if breached else 'No prazo') if done else ('Atrasado' if breached else 'Pausado' if paused else 'Em risco' if risk else 'Dentro do prazo')
    suffix = ' úteis' if calendar.get('mode') == 'business' else ''
    clock = 'Concluído em '+duration(used*60)+suffix if done else ('Atraso de ' if breached else 'Restam ')+duration(abs(remaining)*60)+suffix
    return {'state':state,'label':label,'due':due,'done':done,'clock':clock,'paused':paused,
            'remaining':remaining,'used':used,'progress':min(100,max(0,round(used/max(1,target)*100)))}


def ticket_sla(ticket, now=None):
    now = now or timezone.now()
    response, resolution = phase(ticket,'response',now), phase(ticket,'resolution',now)
    if ticket.status == 'cancelled':
        state,label = 'cancelled','Cancelado'
    elif not ticket.sla_started_at:
        state,label = 'legacy','Sem SLA iniciado'
    elif 'breached' in (response['state'],resolution['state']):
        state,label = 'breached','SLA violado'
    elif ticket.sla_paused_at:
        state,label = 'paused','SLA pausado'
    elif 'risk' in (response['state'],resolution['state']):
        state,label = 'risk','Em risco'
    else:
        state,label = 'ok','Dentro do SLA'
    return {'state':state,'label':label,'response':response,'resolution':resolution,
            'calendar_label':'Horário útil · Brasília' if ticket.sla_calendar.get('mode') == 'business' else '24×7',
            'paused':bool(ticket.sla_paused_at)}
