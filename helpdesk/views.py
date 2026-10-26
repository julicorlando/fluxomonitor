import csv
import hashlib
import json
import logging
import re
from datetime import timedelta
from uuid import uuid4
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .forms import ContactForm, GrantForm, UpdateForm, SlaPolicyForm, DashboardFilterForm
from .models import AgentGrant, BotConfig, Company, Contact, Conversation, Inbound, Outbound, Ticket, UnresolvedInbound, VerifiedChatIdentity, SenderIdentity, SlaPolicy, Satisfaction
from .services import OPEN, audit, company_ids, update_ticket, menu
from .transport import receive_event, valid_signature
from .bindings import remember_sender, import_pending
from .sla import ticket_sla
from .reporting import dashboard


def visible(user):
    return Ticket.objects.filter(company_id__in=company_ids(user))


def version(user, ticket_id=None):
    tickets = visible(user)
    if ticket_id:
        tickets = tickets.filter(pk=ticket_id)
    data = list(tickets.order_by('-updated_at').values_list('pk', 'revision', 'updated_at')[:200])
    data += list(Outbound.objects.filter(company_id__in=company_ids(user), ticket__in=tickets).values('status').annotate(n=Count('id')).order_by('status'))
    data += list(Satisfaction.objects.filter(ticket__in=tickets).values_list('id', 'rating', 'answered_at', 'cancelled', 'feedback'))
    data += list(SlaPolicy.objects.values_list('priority', 'response_minutes', 'resolution_minutes'))
    data += [timezone.now().strftime('%Y%m%d%H%M')]
    return hashlib.sha256(str(data).encode()).hexdigest()


@login_required
@user_passes_test(lambda u: u.is_staff)
@never_cache
def home(request):
    tickets = visible(request.user).select_related('company', 'contact', 'assigned_to')
    companies = Company.objects.filter(pk__in=company_ids(request.user))
    if request.GET.get('company'):
        tickets = tickets.filter(company__code=request.GET['company'])
    if request.GET.get('status') == 'active':
        tickets = tickets.filter(status__in=OPEN)
    elif request.GET.get('status'):
        tickets = tickets.filter(status=request.GET['status'])
    if request.GET.get('q'):
        q = request.GET['q'][:150]
        tickets = tickets.filter(Q(code__icontains=q) | Q(requester_name__icontains=q) | Q(target__icontains=q))
    if request.GET.get('priority') in dict(SlaPolicy.PRIORITIES):
        tickets = tickets.filter(priority=request.GET['priority'])
    if request.GET.get('category') in dict(Ticket.CATEGORIES):
        tickets = tickets.filter(category=request.GET['category'])
    if request.GET.get('assignment') == 'mine':
        tickets = tickets.filter(assigned_to=request.user)
    elif request.GET.get('assignment') == 'unassigned':
        tickets = tickets.filter(assigned_to__isnull=True)
    period = DashboardFilterForm(request.GET)
    if period.is_valid():
        if period.cleaned_data.get('start'):
            tickets = tickets.filter(created_at__date__gte=period.cleaned_data['start'])
        if period.cleaned_data.get('end'):
            tickets = tickets.filter(created_at__date__lte=period.cleaned_data['end'])
    else:
        tickets = tickets.none()
    if request.GET.get('sla') in ('breached', 'risk', 'legacy'):
        sla_state = request.GET['sla']
        matching = [t.pk for t in tickets.filter(status__in=OPEN).iterator(chunk_size=200) if ticket_sla(t)['state'] == sla_state]
        tickets = tickets.filter(pk__in=matching)
    if request.GET.get('format') == 'csv' and period.is_valid():
        return export_tickets(request, tickets)
    all_tickets = tickets
    cfg = BotConfig.objects.get(pk=1)
    worker_ok = bool(cfg.worker_at and cfg.worker_at > timezone.now() - timedelta(seconds=90))
    filters = request.GET.copy()
    filters.pop('page', None)
    page = Paginator(tickets, 30).get_page(request.GET.get('page'))
    for ticket in page:
        ticket.sla = ticket_sla(ticket)
    return render(request, 'helpdesk/home.html', {
        'tickets': page, 'dash': dashboard(all_tickets), 'period': period,
        'priorities': SlaPolicy.PRIORITIES, 'categories': Ticket.CATEGORIES,
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
                          expected_revision=data['revision'], assigned_to=data['assigned_to'],
                          priority=data.get('priority') or None, category=data.get('category') or None,
                          start_sla=data.get('start_sla', False))
            messages.success(request, 'Atualização registrada.' if data['internal'] else 'Atualização registrada e mensagem colocada na fila do WhatsApp.')
            return redirect(f'/support/tickets/{ticket.pk}/')
        except ValidationError as exc:
            form.add_error(None, exc)
    ticket.sla = ticket_sla(ticket)
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
        sla = ticket_sla(ticket)
        stamp = lambda value: value.isoformat() if value else None
        return {'id': ticket.pk, 'code': ticket.code, 'company': ticket.company.code,
                'requester': {'name': ticket.requester_name, 'phone': ticket.requester_phone, 'email': ticket.requester_email},
                'target': ticket.target, 'description': ticket.description, 'status': ticket.status,
                'priority': ticket.priority, 'category': ticket.category,
                'sla': {'state': sla['state'], 'label': sla['label'], 'calendar': '24x7',
                        'started_at': stamp(ticket.sla_started_at), 'response_due_at': stamp(ticket.response_due_at),
                        'resolution_due_at': stamp(ticket.resolution_due_at),
                        'first_responded_at': stamp(ticket.first_responded_at), 'resolved_at': stamp(ticket.resolved_at)},
                'revision': ticket.revision, 'assigned_to': ticket.assigned_to_id,
                'created_at': ticket.created_at.isoformat(), 'updated_at': ticket.updated_at.isoformat()}
    if ticket_id is not None:
        ticket = get_object_or_404(tickets, pk=ticket_id)
        data = serialize(ticket)
        data['evaluations'] = list(ticket.surveys.values('cycle', 'rating', 'feedback', 'answered_at', 'cancelled'))
        data['updates'] = list(ticket.updates.values('id', 'author', 'text', 'internal', 'old_status', 'new_status', 'created_at'))
        return JsonResponse(data)
    if request.GET.get('company'):
        tickets = tickets.filter(company__code=request.GET['company'])
    if request.GET.get('status') == 'active':
        tickets = tickets.filter(status__in=OPEN)
    elif request.GET.get('status'):
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
                               expected_revision=data['revision'], assigned_to=data['assigned_to'],
                          priority=data.get('priority') or None, category=data.get('category') or None,
                          start_sla=data.get('start_sla', False))
    except ValidationError as exc:
        return JsonResponse({'errors': exc.messages}, status=409)
    return JsonResponse({'id': ticket.pk, 'code': ticket.code, 'status': ticket.status, 'revision': ticket.revision})


@login_required
@user_passes_test(lambda u: u.is_active and u.is_superuser)
@never_cache
def master_inbox(request, contact_id=None):
    """Restricted conversation console. No external WhatsApp request on the HTTP thread."""
    contacts = Contact.objects.filter(active=True).select_related('company').order_by('name')
    selected = get_object_or_404(contacts, pk=contact_id) if contact_id else None
    if request.method == 'POST':
        if not selected:
            return HttpResponse(status=400)
        action = request.POST.get('action', 'reply')
        if action == 'verify_lid':
            lid = request.POST.get('lid', '')
            # The operator must independently verify identity, e.g. direct phone call.
            confirmed = request.POST.get('identity_confirmed') == 'yes'
            if not confirmed or not re.fullmatch(r'[0-9]+@lid', lid):
                messages.error(request, 'Confirme a identidade do titular por canal independente e informe um LID válido.')
                return redirect(f'/support/inbox/{selected.pk}/')
            try:
                with transaction.atomic():
                    contact = Contact.objects.select_for_update().get(pk=selected.pk)
                    has_pending = UnresolvedInbound.objects.filter(sender=lid, status='unresolved',
                        sent_at__gte=timezone.now() - timedelta(hours=24)).exists()
                    conflicts = VerifiedChatIdentity.objects.filter(chat_id=lid).exclude(contact=contact).exists()
                    if not has_pending or conflicts:
                        raise ValidationError('Vínculo não permitido: identificador já associado ou nenhuma mensagem recente pendente.')
                    binding = remember_sender(contact, lid, source='master', actor=request.user.get_username())
                    imported = import_pending(binding)
                    audit(request.user.get_username(), 'support_lid_verified', contact,
                          {'lid': lid, 'contact_id': contact.pk, 'phone': contact.phone, 'messages_imported': imported,
                           'verification': 'master_confirmed_out_of_band', 'persistent_binding': binding.pk})
                    messages.success(request, f'Vínculo permanente salvo. {imported} mensagem(ns) encaminhada(s) ao bot. As próximas respostas serão reconhecidas automaticamente.')
            except ValidationError as exc:
                messages.error(request, ' '.join(exc.messages))
            return redirect(f'/support/inbox/{selected.pk}/')
        if action == 'restart_bot':
            with transaction.atomic():
                contact = Contact.objects.select_for_update().select_related('company').get(pk=selected.pk)
                if not BotConfig.objects.filter(pk=1, enabled=True).exists():
                    messages.error(request, 'Ative o atendimento automático nas configurações antes de reiniciar.')
                elif not contact.active:
                    messages.error(request, 'Colaborador inativo; o bot não foi iniciado.')
                else:
                    session, _ = Conversation.objects.get_or_create(contact=contact, defaults={'company': contact.company})
                    session = Conversation.objects.select_for_update().get(pk=session.pk)
                    session.company = contact.company
                    session.state, session.draft, session.expires_at = 'offer', {}, timezone.now() + timedelta(hours=2)
                    session.save(update_fields=['company', 'state', 'draft', 'expires_at', 'updated_at'])
                    Outbound.objects.create(
                        contact=contact, company=contact.company, phone=contact.phone,
                        text=menu(contact), key='master-bot-menu:' + uuid4().hex)
                    audit(request.user.get_username(), 'support_bot_restart', session, {'contact_id': contact.pk})
                    messages.success(request, 'Bot reativado. O menu de atendimento foi colocado na fila do WhatsApp.')
            return redirect(f'/support/inbox/{selected.pk}/')
        if action != 'reply':
            return HttpResponse(status=400)
        body = request.POST.get('text', '').strip()
        if not body or len(body) > 4000:
            messages.error(request, 'Informe uma resposta de até 4.000 caracteres.')
        else:
            with transaction.atomic():
                contact = Contact.objects.select_for_update().get(pk=selected.pk)
                if not contact.active or not BotConfig.objects.filter(pk=1, enabled=True).exists():
                    messages.error(request, 'Colaborador inativo ou atendimento pausado. Nada foi enviado.')
                else:
                    Outbound.objects.create(
                        contact=contact, company=contact.company, phone=contact.phone,
                        text=body, key='master:' + uuid4().hex)
                    messages.success(request, 'Resposta colocada na fila do WhatsApp.')
            return redirect(f'/support/inbox/{selected.pk}/')
    incoming = Inbound.objects.filter(contact=selected).order_by('-id')[:100] if selected else []
    outgoing = Outbound.objects.filter(contact=selected).order_by('-id')[:100] if selected else []
    history = sorted(
        [(m.sent_at, 'in', m) for m in incoming] +
        [(m.created_at, 'out', m) for m in outgoing],
        key=lambda item: (item[0], item[2].pk))
    conversation = Conversation.objects.filter(contact=selected).first() if selected else None
    inbound_pending = Inbound.objects.filter(contact=selected, processed_at__isnull=True).count() if selected else 0
    unresolved = UnresolvedInbound.objects.filter(status='unresolved').order_by('-received_at')[:100]
    return render(request, 'helpdesk/master_inbox.html', {
        'unresolved': unresolved, 'unresolved_count': UnresolvedInbound.objects.filter(status='unresolved').count(),
        'contacts': contacts, 'selected': selected, 'history': history,
        'conversation': conversation, 'inbound_pending': inbound_pending,
        'sender_bindings': SenderIdentity.objects.filter(contact=selected, phone=selected.phone) if selected else [],
    })


def export_tickets(request, tickets):
    # Only called after home() has applied company permissions and validated filters.
    class Echo:
        def write(self, value):
            return value
    from django.db.models import OuterRef, Subquery
    tickets = tickets.annotate(last_rating=Subquery(Satisfaction.objects.filter(ticket_id=OuterRef('pk'),
        cancelled=False, answered_at__isnull=False).order_by('-answered_at').values('rating')[:1]))
    writer = csv.writer(Echo(), delimiter=';')
    def safe(value):
        text = str(value or '')
        return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text
    def rows():
        yield '\ufeff'
        yield writer.writerow(['Protocolo','Empresa','Colaborador','Equipamento / setor','Categoria','Prioridade',
            'Status','Responsável','Abertura','Início SLA','Prazo resposta','Primeira resposta','Prazo resolução','Resolução','SLA','Última nota'])
        for t in tickets.iterator(chunk_size=200):
            stamp = lambda d: timezone.localtime(d).strftime('%d/%m/%Y %H:%M') if d else ''
            yield writer.writerow([safe(value) for value in [t.code,t.company.code,t.requester_name,t.target,
                t.get_category_display(),t.get_priority_display(),t.get_status_display(),t.assigned_to,
                stamp(t.created_at),stamp(t.sla_started_at),stamp(t.response_due_at),stamp(t.first_responded_at),
                stamp(t.resolution_due_at),stamp(t.resolved_at),ticket_sla(t)['label'],t.last_rating]])
    response = StreamingHttpResponse(rows(), content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="gesti-ti-chamados.csv"'
    response['Cache-Control'] = 'no-store'
    return response


@login_required
@user_passes_test(lambda u: u.is_superuser)
@never_cache
def sla_settings(request):
    from django.forms import modelformset_factory
    Factory = modelformset_factory(SlaPolicy, form=SlaPolicyForm, extra=0, edit_only=True)
    forms = Factory(request.POST or None, queryset=SlaPolicy.objects.order_by('id'))
    if request.method == 'POST' and forms.is_valid():
        # Ensure all four policies are submitted exactly once; IDs cannot remove a policy.
        ids = [form.cleaned_data['id'].pk for form in forms if form.cleaned_data.get('id')]
        expected = set(SlaPolicy.objects.values_list('pk', flat=True))
        if len(ids) != len(expected) or set(ids) != expected:
            messages.error(request, 'Envie as quatro prioridades sem duplicar ou excluir regras.')
        else:
            with transaction.atomic():
                list(SlaPolicy.objects.select_for_update().all())
                for form in forms:
                    if form.has_changed():
                        policy = form.save()
                        audit(request.user.get_username(), 'support_sla_policy', policy,
                              {'priority':policy.priority, 'response_minutes':policy.response_minutes,
                               'resolution_minutes':policy.resolution_minutes})
            messages.success(request, 'Prazos salvos. Chamados já iniciados preservam seus prazos; novos chamados usam as novas metas.')
            return redirect('/support/sla/')
    return render(request, 'helpdesk/sla_settings.html', {'formset': forms})
