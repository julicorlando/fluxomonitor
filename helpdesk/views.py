import hashlib
import json
import logging
from datetime import timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .forms import ContactForm, GrantForm, UpdateForm
from .models import AgentGrant, BotConfig, Company, Contact, Conversation, Inbound, Outbound, Ticket
from .services import OPEN, audit, company_ids, update_ticket
from .transport import receive_event, valid_signature


def visible(user):
    return Ticket.objects.filter(company_id__in=company_ids(user))


def version(user, ticket_id=None):
    tickets = visible(user)
    if ticket_id:
        tickets = tickets.filter(pk=ticket_id)
    data = list(tickets.order_by('-updated_at').values_list('pk', 'revision', 'updated_at')[:200])
    data += list(Outbound.objects.filter(company_id__in=company_ids(user), ticket__in=tickets).values('status').annotate(n=Count('id')).order_by('status'))
    return hashlib.sha256(str(data).encode()).hexdigest()


@login_required
@user_passes_test(lambda u: u.is_staff)
@never_cache
def home(request):
    tickets = visible(request.user).select_related('company', 'contact', 'assigned_to')
    companies = Company.objects.filter(pk__in=company_ids(request.user))
    if request.GET.get('company'):
        tickets = tickets.filter(company__code=request.GET['company'])
    if request.GET.get('status'):
        tickets = tickets.filter(status=request.GET['status'])
    if request.GET.get('q'):
        q = request.GET['q'][:150]
        tickets = tickets.filter(Q(code__icontains=q) | Q(requester_name__icontains=q) | Q(target__icontains=q))
    all_tickets = visible(request.user)
    cfg = BotConfig.objects.get(pk=1)
    worker_ok = bool(cfg.worker_at and cfg.worker_at > timezone.now() - timedelta(seconds=90))
    filters = request.GET.copy()
    filters.pop('page', None)
    return render(request, 'helpdesk/home.html', {
        'tickets': Paginator(tickets, 30).get_page(request.GET.get('page')),
        'companies': companies, 'statuses': Ticket.STATUS, 'cfg': cfg, 'worker_ok': worker_ok,
        'open_count': all_tickets.filter(status__in=OPEN).count(),
        'resolved_count': all_tickets.filter(status__in=('resolved', 'closed')).count(),
        'waiting_count': all_tickets.filter(status='waiting').count(), 'version': version(request.user),
        'filters': filters.urlencode(),
        'contact_count': Contact.objects.filter(company__in=companies, active=True).count(),
        'send_issues': Outbound.objects.filter(company__in=companies, status__in=('failed', 'unknown', 'blocked')).count(),
        'inbox_issues': Inbound.objects.filter(company__in=companies, processed_at__isnull=True, attempts__gte=5).count(),
    })


@login_required
@user_passes_test(lambda u: u.is_staff)
@never_cache
def pulse(request):
    ticket_id = request.GET.get('ticket')
    if ticket_id:
        if not ticket_id.isascii() or not ticket_id.isdigit() or len(ticket_id) > 18:
            return JsonResponse({'error': 'Chamado inválido.'}, status=400)
        ticket = get_object_or_404(visible(request.user), pk=ticket_id)
        ticket_id = ticket.pk
    return JsonResponse({'version': version(request.user, ticket_id)})


@login_required
@user_passes_test(lambda u: u.is_staff)
@never_cache
def detail(request, ticket_id):
    ticket = get_object_or_404(visible(request.user).select_related('company', 'contact', 'assigned_to'), pk=ticket_id)
    form = UpdateForm(request.POST or None, ticket=ticket)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            update_ticket(ticket.pk, request.user, status=data['status'], text=data['text'], internal=data['internal'],
                          expected_revision=data['revision'], assigned_to=data['assigned_to'])
            messages.success(request, 'Atualização registrada.' if data['internal'] else 'Atualização registrada e mensagem colocada na fila do WhatsApp.')
            return redirect(f'/support/tickets/{ticket.pk}/')
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, 'helpdesk/detail.html', {'ticket': ticket, 'form': form,
        'updates': ticket.updates.all(), 'deliveries': ticket.messages.order_by('-id')[:30],
        'surveys': ticket.surveys.order_by('-id'), 'version': version(request.user, ticket.pk)})


@login_required
@user_passes_test(lambda u: u.is_superuser)
@never_cache
def contacts(request, contact_id=None):
    contact = get_object_or_404(Contact, pk=contact_id) if contact_id else None
    form = ContactForm(request.POST or None, instance=contact)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            if contact:
                Contact.objects.select_for_update().get(pk=contact.pk)
            saved = form.save()
            if contact and {'phone', 'company', 'active'}.intersection(form.changed_data):
                Conversation.objects.filter(contact=saved).delete()
            audit(request.user.get_username(), 'support_contact', saved, {'company': saved.company.code, 'active': saved.active})
        messages.success(request, 'Colaborador salvo. O atendimento reconhece somente números ativos cadastrados.')
        return redirect('/support/contacts/')
    return render(request, 'helpdesk/contacts.html', {'form': form, 'editing': contact,
        'contacts': Paginator(Contact.objects.select_related('company'), 50).get_page(request.GET.get('page'))})


@login_required
@user_passes_test(lambda u: u.is_superuser)
@never_cache
def settings(request):
    cfg = BotConfig.objects.get(pk=1)
    grants = GrantForm(request.POST if request.method == 'POST' and request.POST.get('action') == 'grant' else None)
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'toggle':
            with transaction.atomic():
                cfg = BotConfig.objects.select_for_update().get(pk=1)
                cfg.enabled = request.POST.get('enabled') == '1'
                cfg.save(update_fields=['enabled'])
                audit(request.user.get_username(), 'support_bot', cfg, {'enabled': cfg.enabled})
            messages.success(request, 'Atendimento ativado. O recebimento será conferido automaticamente.' if cfg.enabled else 'Atendimento pausado. Mensagens novas serão ignoradas.')
            return redirect('/support/settings/')
        if action == 'grant' and grants.is_valid():
            user = grants.cleaned_data['user']
            with transaction.atomic():
                AgentGrant.objects.filter(user=user).delete()
                AgentGrant.objects.bulk_create([AgentGrant(user=user, company=c) for c in grants.cleaned_data['companies']])
                audit(request.user.get_username(), 'support_access', user, {'companies': [c.code for c in grants.cleaned_data['companies']]})
            messages.success(request, 'Permissões substituídas pelas empresas selecionadas.')
            return redirect('/support/settings/')
    return render(request, 'helpdesk/settings.html', {'cfg': cfg, 'grant_form': grants,
        'grants': AgentGrant.objects.select_related('user', 'company').order_by('user__username', 'company__code')})


@login_required
@user_passes_test(lambda u: u.is_staff)
@never_cache
def deliveries(request):
    ids = company_ids(request.user)
    return render(request, 'helpdesk/deliveries.html', {
        'deliveries': Paginator(Outbound.objects.filter(company_id__in=ids).select_related('contact', 'ticket').order_by('-id'), 50).get_page(request.GET.get('page')),
        'issues': Inbound.objects.filter(company_id__in=ids, processed_at__isnull=True, attempts__gte=5).select_related('contact')[:30],
    })


@csrf_exempt
@require_POST
def webhook(request):
    if len(request.body) > 1048576:
        return HttpResponse(status=413)
    if not valid_signature(request.body, request.headers.get('X-Webhook-Hmac', ''), request.headers.get('X-Webhook-Hmac-Algorithm', '')):
        return HttpResponse(status=403)
    try:
        event = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=400)
    if not isinstance(event, dict):
        return HttpResponse(status=400)
    try:
        receive_event(event)
    except Exception:
        logging.getLogger(__name__).exception('Falha ao persistir webhook do suporte')
        return HttpResponse(status=503)
    return HttpResponse(status=204)


@login_required
@user_passes_test(lambda u: u.is_staff)
@never_cache
def tickets_api(request, ticket_id=None):
    if request.method != 'GET':
        return HttpResponse(status=405)
    tickets = visible(request.user).select_related('company')
    def serialize(ticket):
        return {'id': ticket.pk, 'code': ticket.code, 'company': ticket.company.code,
                'requester': {'name': ticket.requester_name, 'phone': ticket.requester_phone, 'email': ticket.requester_email},
                'target': ticket.target, 'description': ticket.description, 'status': ticket.status,
                'revision': ticket.revision, 'assigned_to': ticket.assigned_to_id,
                'created_at': ticket.created_at.isoformat(), 'updated_at': ticket.updated_at.isoformat()}
    if ticket_id is not None:
        ticket = get_object_or_404(tickets, pk=ticket_id)
        data = serialize(ticket)
        data['updates'] = list(ticket.updates.values('id', 'author', 'text', 'internal', 'old_status', 'new_status', 'created_at'))
        return JsonResponse(data)
    if request.GET.get('company'):
        tickets = tickets.filter(company__code=request.GET['company'])
    if request.GET.get('status'):
        tickets = tickets.filter(status=request.GET['status'])
    page = Paginator(tickets, 50).get_page(request.GET.get('page'))
    return JsonResponse({'count': page.paginator.count, 'page': page.number, 'pages': page.paginator.num_pages,
                         'results': [serialize(ticket) for ticket in page]})


@login_required
@user_passes_test(lambda u: u.is_staff)
@require_POST
@never_cache
def update_api(request, ticket_id):
    ticket = get_object_or_404(visible(request.user), pk=ticket_id)
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'JSON inválido.'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': 'Envie um objeto JSON.'}, status=400)
    form = UpdateForm(data, ticket=ticket)
    if not form.is_valid():
        return JsonResponse({'errors': form.errors.get_json_data()}, status=400)
    data = form.cleaned_data
    try:
        ticket = update_ticket(ticket.pk, request.user, status=data['status'], text=data['text'], internal=data['internal'],
                               expected_revision=data['revision'], assigned_to=data['assigned_to'])
    except ValidationError as exc:
        return JsonResponse({'errors': exc.messages}, status=409)
    return JsonResponse({'id': ticket.pk, 'code': ticket.code, 'status': ticket.status, 'revision': ticket.revision})
