"""Scoped reporting: every query starts with the caller's permitted ticket set."""
from collections import Counter
from datetime import timedelta
from django.db.models import Avg, Count, F, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from .models import Satisfaction, Ticket, SlaPolicy
from .sla import duration, ticket_sla

ACTIVE = ('open', 'working', 'waiting')


def dashboard(tickets):
    now = timezone.now()
    active = tickets.filter(status__in=ACTIVE)
    tracked = tickets.filter(sla_started_at__isnull=False).exclude(status='cancelled')
    response_eligible = tracked.filter(Q(first_responded_at__isnull=False) | Q(response_due_at__lt=now))
    resolution_eligible = tracked.filter(Q(resolved_at__isnull=False) | Q(resolution_due_at__lt=now))
    response_met = response_eligible.filter(first_responded_at__lte=F('response_due_at')).count()
    resolution_met = resolution_eligible.filter(resolved_at__lte=F('resolution_due_at')).count()
    response_n, resolution_n = response_eligible.count(), resolution_eligible.count()
    times = tracked.aggregate(response=Avg(F('first_responded_at')-F('sla_started_at')),
                              resolution=Avg(F('resolved_at')-F('sla_started_at')))
    surveys = Satisfaction.objects.filter(ticket__in=tickets, cancelled=False)
    answered = surveys.filter(answered_at__isnull=False, rating__isnull=False)
    ratings = answered.aggregate(total=Count('id'), average=Avg('rating'), happy=Count('id', filter=Q(rating__gte=4)))
    dist = dict(answered.values('rating').annotate(n=Count('id')).values_list('rating', 'n'))
    total_ratings = ratings['total']
    attention, active_states = [], Counter()
    priority_rank = {'critical': 0, 'high': 1, 'normal': 2, 'low': 3}
    for ticket in active.select_related('company', 'assigned_to').iterator(chunk_size=200):
        ticket.sla = ticket_sla(ticket, now)
        active_states[ticket.sla['state']] += 1
        rank = {'breached': 0, 'risk': 1, 'legacy': 3, 'ok': 2}[ticket.sla['state']]
        attention.append((rank, priority_rank[ticket.priority], ticket.created_at, ticket))
        attention.sort(key=lambda row: row[:3])
        del attention[8:]
    # Keep only the operational summary in memory after calculating exact counters.
    attention = [row[3] for row in attention[:8]]
    company_rows = list(tickets.values('company__code').annotate(total=Count('id'),
        active=Count('id', filter=Q(status__in=ACTIVE)), closed=Count('id',filter=Q(status__in=('resolved','closed')))).order_by('company__code'))
    workload = list(active.values('assigned_to__username').annotate(total=Count('id'),
        critical=Count('id',filter=Q(priority='critical'))).order_by('-total', 'assigned_to__username'))
    today = timezone.localdate()
    days = [today-timedelta(days=i) for i in range(13,-1,-1)]
    counts = dict(tickets.filter(created_at__date__gte=days[0], created_at__date__lte=today)
        .annotate(day=TruncDate('created_at')).values('day').annotate(n=Count('id')).values_list('day','n'))
    maximum = max(counts.values(), default=1) or 1
    priority_counts = dict(tickets.values('priority').annotate(n=Count('id')).values_list('priority','n'))
    status_counts = dict(tickets.values('status').annotate(n=Count('id')).values_list('status','n'))
    category_rows = list(tickets.values('category').annotate(total=Count('id')).order_by('-total'))
    for row in category_rows:
        row['label'] = dict(Ticket.CATEGORIES).get(row['category'], row['category'])
    return {
        'total': tickets.count(), 'active': active.count(), 'unassigned': active.filter(assigned_to__isnull=True).count(),
        'resolved': tickets.filter(status__in=('resolved','closed')).count(),
        'waiting': active.filter(status='waiting').count(), 'critical': active.filter(priority='critical').count(),
        'overdue': active_states['breached'], 'risk': active_states['risk'], 'legacy': active_states['legacy'],
        'response_met': response_met, 'response_n': response_n,
        'resolution_met': resolution_met, 'resolution_n': resolution_n,
        'response_pct': round(response_met/response_n*100,1) if response_n else None,
        'resolution_pct': round(resolution_met/resolution_n*100,1) if resolution_n else None,
        'response_avg': duration(times['response'].total_seconds()) if times['response'] is not None else '—',
        'resolution_avg': duration(times['resolution'].total_seconds()) if times['resolution'] is not None else '—',
        'rating_avg': round(ratings['average'],1) if total_ratings else None,
        'rating_count': total_ratings,
        'csat': round(ratings['happy']/total_ratings*100,1) if total_ratings else None,
        'survey_pending': surveys.filter(answered_at__isnull=True).count(),
        'survey_rate': round(total_ratings/surveys.count()*100,1) if surveys.exists() else None,
        'ratings': [{'score':score,'n':dist.get(score,0), 'width':round(dist.get(score,0)/total_ratings*100) if total_ratings else 0} for score in range(5,0,-1)],
        'recent_ratings': answered.select_related('ticket').order_by('-answered_at')[:6],
        'pending_ratings': surveys.filter(answered_at__isnull=True).select_related('ticket').order_by('created_at')[:6],
        'attention': attention, 'companies': company_rows, 'workload': workload,
        'trend': [{'day':day,'count':counts.get(day,0),'height':max(2,round(counts.get(day,0)/maximum*100))} for day in days],
        'priorities': [{'label':label,'key':key,'n':priority_counts.get(key,0)} for key,label in SlaPolicy.PRIORITIES],
        'statuses': [{'label':label,'key':key,'n':status_counts.get(key,0)} for key,label in Ticket.STATUS],
        'categories': category_rows,
    }
