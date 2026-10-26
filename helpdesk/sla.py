"""24x7 SLA, immutable deadlines until explicit reprioritization or reopening."""
from datetime import timedelta
from django.utils import timezone
from .models import SlaPolicy


def start_sla(ticket, now=None):
    now = now or timezone.now()
    policy = SlaPolicy.objects.get(priority=ticket.priority)
    ticket.sla_started_at = now
    ticket.response_due_at = now + timedelta(minutes=policy.response_minutes)
    ticket.resolution_due_at = now + timedelta(minutes=policy.resolution_minutes)
    ticket.first_responded_at = None


def reprioritize(ticket):
    policy = SlaPolicy.objects.get(priority=ticket.priority)
    ticket.response_due_at = ticket.sla_started_at + timedelta(minutes=policy.response_minutes)
    ticket.resolution_due_at = ticket.sla_started_at + timedelta(minutes=policy.resolution_minutes)


def duration(seconds):
    if seconds is None:
        return '—'
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f'{minutes} min'
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    return f'{days}d {hours}h' if days else f'{hours}h {minutes:02d}min'


def phase(start, due, done, now):
    if not start or not due:
        return {'state': 'legacy', 'label': 'Não iniciado', 'progress': 0, 'clock': 'Sem prazo anterior'}
    point = done or now
    breached = point > due
    target = max(1, (due - start).total_seconds())
    remaining = (due - point).total_seconds()
    risk = not done and not breached and remaining <= min(3600, target * .2)
    state = 'breached' if breached else ('risk' if risk else 'ok')
    label = ('Fora do prazo' if breached else 'No prazo') if done else ('Atrasado' if breached else 'Em risco' if risk else 'Dentro do prazo')
    clock = ('Concluído em ' + duration((done-start).total_seconds())) if done else (('Atraso de ' if breached else 'Restam ') + duration(abs(remaining)))
    return {'state': state, 'label': label, 'due': due, 'done': done, 'clock': clock,
            'progress': min(100, max(0, round((point-start).total_seconds() / target * 100)))}


def ticket_sla(ticket, now=None):
    now = now or timezone.now()
    response = phase(ticket.sla_started_at, ticket.response_due_at, ticket.first_responded_at, now)
    resolution = phase(ticket.sla_started_at, ticket.resolution_due_at, ticket.resolved_at, now)
    if ticket.status == 'cancelled':
        return {'state': 'cancelled', 'label': 'Cancelado', 'response': response, 'resolution': resolution}
    if not ticket.sla_started_at:
        state, label = 'legacy', 'Sem SLA iniciado'
    elif 'breached' in (response['state'], resolution['state']):
        state, label = 'breached', 'SLA violado'
    elif 'risk' in (response['state'], resolution['state']):
        state, label = 'risk', 'Em risco'
    else:
        state, label = 'ok', 'Dentro do SLA'
    return {'state': state, 'label': label, 'response': response, 'resolution': resolution}
