"""Transactional help desk rules. Network calls belong to transport.py only."""
import re
import unicodedata
from datetime import timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from flow.models import AuditLog
from .models import (AgentGrant, BotConfig, Company, Contact, Conversation, Inbound,
                     Outbound, Satisfaction, Ticket, TicketUpdate, Asset, Problem, KnowledgeArticle, KnowledgeSuggestion)
from .sla import start_sla as initialize_sla, reprioritize, waiting_transition

STATUS = dict(Ticket.STATUS)
OPEN = ('open', 'working', 'waiting')
TRANSITIONS = {
    'open': {'working', 'waiting', 'resolved', 'cancelled'},
    'working': {'waiting', 'resolved', 'cancelled'},
    'waiting': {'working', 'resolved', 'cancelled'},
    'resolved': {'working', 'closed'},
    'closed': {'working'}, 'cancelled': {'open'},
}


def audit(actor, action, obj, changes):
    AuditLog.objects.create(actor=str(actor)[:150], action=action, object_type=type(obj).__name__,
                            object_id=str(obj.pk), changes=changes)


def company_ids(user):
    if not user.is_active or not user.is_staff:
        return Company.objects.none().values_list('pk', flat=True)
    if user.is_superuser:
        return Company.objects.values_list('pk', flat=True)
    return AgentGrant.objects.filter(user=user).values_list('company_id', flat=True)


def queue(contact, text, key, ticket=None, company_id=None, phone=None):
    return Outbound.objects.get_or_create(key=key, defaults={
        'contact': contact, 'company_id': company_id or contact.company_id,
        'phone': phone or contact.phone, 'text': text, 'ticket': ticket,
    })[0]


def reply(message, text, ticket=None):
    return queue(message.contact, text, f'inbound:{message.pk}', ticket, message.company_id, message.phone)


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.strip().lower()) if not unicodedata.combining(c))


def create_ticket(session, message):
    if pending_surveys(message.contact).exists():
        raise ValidationError('Avalie o atendimento anterior antes de abrir outro chamado.')
    company = Company.objects.select_for_update().get(pk=session.company_id)
    company.last_ticket += 1
    company.save(update_fields=['last_ticket'])
    contact = message.contact
    ticket = Ticket.objects.create(
        company=company, number=company.last_ticket, code=f'{company.code}-{company.last_ticket:05d}',
        contact=contact, requester_name=contact.name, requester_phone=message.phone,
        requester_email=contact.email, target=session.draft['target'], description=session.draft['description'])
    initialize_sla(ticket, ticket.created_at)
    ticket.save(update_fields=['sla_started_at', 'response_due_at', 'resolution_due_at', 'sla_calendar'])
    TicketUpdate.objects.create(ticket=ticket, author=contact.name, text='Chamado aberto pelo WhatsApp.')
    audit(contact.name, 'support_open', ticket, {'code': ticket.code})
    return ticket


def pending_surveys(contact):
    return Satisfaction.objects.filter(ticket__contact=contact, ticket__company=contact.company,
        ticket__requester_phone=contact.phone, ticket__status__in=('resolved', 'closed'),
        cancelled=False, answered_at__isnull=True).select_related('ticket').order_by('created_at', 'id')


def survey_prompt(survey):
    return (f'Como você avalia o atendimento do chamado *{survey.ticket.code}*?\n'
            'Responda apenas com uma nota de *1 a 5*:\n'
            '1 — Muito insatisfeito\n2 — Insatisfeito\n3 — Regular\n4 — Satisfeito\n5 — Muito satisfeito\n\n'
            'A avaliação é obrigatória para abrir um novo chamado.\n'
            f'Você também pode enviar: AVALIAR {survey.ticket.code} 5')


def knowledge_match(contact, description):
    text = ' '+re.sub(r'[^a-z0-9]+',' ',normalized(description)).strip()+' '
    matches = []
    for article in KnowledgeArticle.objects.filter(company=contact.company,published=True,bot_enabled=True):
        score = sum(1 for keyword in article.keywords.split(',') if len(normalized(keyword).strip()) >= 3 and
                    ' '+re.sub(r'[^a-z0-9]+',' ',normalized(keyword)).strip()+' ' in text)
        if score:
            matches.append((score,article.pk,article))
    return max(matches,key=lambda row:(row[0],-row[1]))[2] if matches else None


def confirm_draft(session, contact):
    return (f'Confira os dados:\nEmpresa: {contact.company.code}\nColaborador: {contact.name}\n'
            f'Setor/equipamento: {session.draft["target"]}\nProblema: {session.draft["description"]}\n\n'
            '1 — Confirmar abertura\n2 — Corrigir\n3 — Cancelar')


def requester_note(ticket, contact, text, now):
    previous = ticket.status
    if previous == 'waiting':
        ticket.status = 'working'
        waiting_transition(ticket,previous,now)
    ticket.revision += 1
    ticket.updated_at = now
    ticket.save()
    TicketUpdate.objects.create(ticket=ticket,author=contact.name,text=text,old_status=previous,new_status=ticket.status)
    audit(contact.name,'support_note_resume',ticket,{'from':previous,'to':ticket.status,'sla_resumed':previous=='waiting'})


def menu(contact):
    survey = pending_surveys(contact).first()
    if survey:
        return survey_prompt(survey)
    existing = list(Ticket.objects.filter(contact=contact, company=contact.company, status__in=OPEN).values_list('code', flat=True)[:5])
    if existing:
        return ('Você tem chamados em aberto: ' + ', '.join(existing) +
                '.\n1 — Abrir novo chamado\n2 — Consultar andamento\n3 — Complementar chamado\n4 — Falar com atendente\nOu envie CANCELAR.')
    return (f'Olá, {contact.name}! Identifiquei seu cadastro na {contact.company.code}. '
            'Você entrou em contato com o Suporte de TI. Deseja abrir um novo chamado?\n1 — Sim\n2 — Não\nOu envie ATENDENTE.')


@transaction.atomic
def process_message(message_id):
    # Lock the contact before its session: the same lock is used by all administrative changes.
    initial = Inbound.objects.get(pk=message_id)
    contact = Contact.objects.select_for_update().select_related('company').get(pk=initial.contact_id)
    message = Inbound.objects.select_for_update().get(pk=message_id)
    if message.processed_at:
        return
    message.contact = contact
    now = timezone.now()
    if (not BotConfig.objects.filter(pk=1, enabled=True).exists() or not contact.active or
            message.company_id != contact.company_id or message.phone != contact.phone or
            now - message.sent_at > timedelta(hours=24)):
        message.processed_at = now
        message.error = 'Ignorado: cadastro alterado, inativo, bot pausado ou mensagem antiga.'
        message.save(update_fields=['processed_at', 'error'])
        return
    session, _ = Conversation.objects.get_or_create(contact=contact, defaults={'company': contact.company})
    session = Conversation.objects.select_for_update().get(pk=session.pk)
    if Inbound.objects.filter(contact=contact, id__lt=message.pk, processed_at__isnull=True, attempts__lt=5).exists():
        return
    if session.last_sent_at and message.sent_at < session.last_sent_at:
        message.processed_at, message.error = now, 'Mensagem antiga recebida fora de ordem; não alterou a conversa.'
        message.save(update_fields=['processed_at', 'error'])
        return
    session.last_sent_at = message.sent_at
    if session.company_id != contact.company_id or (session.expires_at and session.expires_at < now):
        session.company_id = contact.company_id
        session.state, session.draft = 'idle', {}
    text = message.text.strip()
    n = normalized(text)
    output, ticket = None, None

    # Pending evaluation takes precedence over every opening path and session state.
    rating = re.fullmatch(r'avaliar\s+([a-z]+-\d+)\s+([1-5])', n)
    surveys = pending_surveys(contact)
    generic_rating = re.fullmatch('[1-5]', n) and surveys.exists()
    # Atendimento humano é um modo silencioso persistente, mesmo quando há
    # avaliação pendente: não iniciar, retomar ou responder pelo chatbot.
    if session.state == 'human':
        pass
    elif n in ('atendente', 'falar com atendente', '4') and (n != '4' or (session.state in ('idle', 'offer') and not surveys.exists())):
        session.state, session.draft = 'human', {}
        audit(contact.name, 'support_human_only', session, {'contact_id': contact.pk})
    elif rating or generic_rating:
        survey = surveys.filter(ticket__code__iexact=rating[1]).first() if rating else surveys.first()
        if survey and int(message.sent_at.timestamp()) < int(survey.created_at.timestamp()):
            output = 'Recebi uma mensagem anterior ao encerramento. Para avaliar agora:\n' + survey_prompt(survey)
        elif survey:
            survey.rating = int(rating[2] if rating else n)
            survey.answered_at = now
            survey.save(update_fields=['rating', 'answered_at'])
            audit(contact.name, 'support_rating', survey, {'rating': survey.rating, 'ticket': survey.ticket.code})
            output = f'Obrigado! Sua nota {survey.rating} para o chamado *{survey.ticket.code}* foi registrada.'
            remaining = pending_surveys(contact).first()
            if remaining:
                output += '\n\nHá outra avaliação pendente.\n' + survey_prompt(remaining)
            else:
                session.state, session.draft = 'offer', {}
                output += (f'\nPara deixar um comentário opcional, envie FEEDBACK {survey.ticket.code} seguido do comentário.'
                           '\n\nAvaliações concluídas. Você já pode abrir um novo chamado.\n' + menu(contact))
        else:
            output = 'Não encontrei uma avaliação pendente desse chamado. Confira o protocolo.'
    elif surveys.exists():
        output = survey_prompt(surveys.first())
    elif n.startswith('feedback '):
        parts = text.split(maxsplit=2)
        survey = Satisfaction.objects.filter(ticket__contact=contact, ticket__company=contact.company,
            ticket__requester_phone=contact.phone, ticket__code__iexact=parts[1], cancelled=False,
            answered_at__isnull=False, expires_at__gt=now).order_by('-id').first() if len(parts) == 3 else None
        if survey and len(parts[2]) <= 2000:
            survey.feedback = parts[2]
            survey.save(update_fields=['feedback'])
            output = f'Comentário registrado no chamado {survey.ticket.code}. Obrigado!'
        else:
            output = 'Use FEEDBACK SIGLA-00001 seguido de um comentário de até 2.000 caracteres, após avaliar o chamado.'
    elif n in ('cancelar', 'recomecar', 'reiniciar'):
        session.state, session.draft = 'idle', {}
        if n == 'cancelar':
            output = 'Conversa encerrada. Seus chamados existentes continuam registrados. Quando precisar, é só chamar!'
        else:
            session.state = 'offer'
            output = menu(contact)
    elif session.state == 'idle':
        session.state, session.draft = 'offer', {}
        output = menu(contact)
    elif session.state == 'offer':
        has_open = Ticket.objects.filter(contact=contact, company=contact.company, status__in=OPEN).exists()
        if n in ('1', 'sim', 'novo', 'abrir chamado'):
            session.state, session.draft = 'target', {}
            output = 'Qual setor ou equipamento está afetado? Exemplo: Financeiro — computador FIN-03.'
        elif n in ('2', 'nao') and (not has_open or n == 'nao'):
            session.state = 'idle'
            output = 'Tudo bem! Quando precisar de suporte, é só enviar uma mensagem.'
        elif has_open and n in ('2', '3'):
            session.state = 'lookup' if n == '2' else 'select_note'
            output = 'Informe o protocolo do chamado. Exemplo: PDU-00121.'
        else:
            output = menu(contact)
    elif session.state == 'target':
        if not 2 <= len(text) <= 200:
            output = 'Descreva o setor ou equipamento em 2 a 200 caracteres.'
        else:
            session.draft['target'] = text
            session.state = 'description'
            output = 'Descreva brevemente o problema e como ele afeta seu trabalho. Não envie senhas.'
    elif session.state == 'description':
        if not 5 <= len(text) <= 4000:
            output = 'Descreva o problema por escrito, usando de 5 a 4.000 caracteres.'
        else:
            session.draft['description'] = text
            session.state = 'confirm'
            article = knowledge_match(contact,text)
            if article:
                suggestion = KnowledgeSuggestion.objects.create(article=article,contact=contact)
                session.state = 'knowledge'
                session.draft['suggestion'] = suggestion.pk
                output = (f'Antes de abrir, este procedimento pode ajudar:\n*{article.title}*\n\n{article.procedure}'
                          '\n\n1 — Resolveu meu problema\n2 — Continuar e abrir chamado\nVocê pode escolher 2 sem executar o procedimento.')
            else:
                output = confirm_draft(session,contact)
    elif session.state == 'knowledge':
        suggestion = KnowledgeSuggestion.objects.filter(pk=session.draft.get('suggestion'),contact=contact,
                                                        article__company=contact.company).first()
        if n in ('1','resolveu','resolvido'):
            if suggestion:
                suggestion.helpful = True
                suggestion.save(update_fields=['helpful'])
            session.state,session.draft = 'idle',{}
            output = 'Ótimo! Registrei que o procedimento resolveu. Quando precisar, é só chamar.'
        elif n in ('2','continuar','abrir chamado','nao'):
            if suggestion:
                suggestion.helpful = False
                suggestion.save(update_fields=['helpful'])
            session.state = 'confirm'
            output = confirm_draft(session,contact)
        else:
            output = 'Responda 1 se resolveu ou 2 para continuar com a abertura do chamado.'
    elif session.state == 'confirm':
        if n in ('1', 'sim', 'confirmar'):
            ticket = create_ticket(session, message)
            session.state, session.draft = 'idle', {}
            output = f'Chamado *{ticket.code}* aberto com sucesso! Nossa equipe já foi notificada.'
        elif n in ('2', 'corrigir'):
            session.state, session.draft = 'target', {}
            output = 'Vamos corrigir. Qual setor ou equipamento está afetado?'
        elif n in ('3', 'nao'):
            session.state, session.draft = 'idle', {}
            output = 'Abertura cancelada. Quando precisar, é só chamar!'
        else:
            output = 'Responda 1 para confirmar, 2 para corrigir ou 3 para cancelar.'
    elif session.state in ('lookup', 'select_note'):
        ticket = Ticket.objects.filter(code__iexact=text, contact=contact, company=contact.company,
                                       requester_phone=contact.phone).first()
        if not ticket:
            output = 'Não encontrei esse chamado no seu cadastro. Confira o protocolo ou envie CANCELAR.'
        elif session.state == 'lookup':
            last = ticket.updates.filter(internal=False).last()
            output = f'{ticket.code} · {ticket.get_status_display()}\n' + (last.text if last else ticket.description)
            session.state, session.draft = 'idle', {}
        elif ticket.status not in OPEN:
            output = 'Esse chamado já foi encerrado. Solicite à equipe a reabertura ou envie CANCELAR para iniciar outra conversa.'
        else:
            session.state, session.draft = 'note', {'ticket': ticket.pk}
            output = f'Envie a informação que deseja acrescentar ao chamado {ticket.code}.'
    elif session.state == 'note':
        ticket = Ticket.objects.filter(pk=session.draft.get('ticket'), contact=contact, company=contact.company,
                                       requester_phone=contact.phone, status__in=OPEN).first()
        if ticket and text:
            requester_note(ticket,contact,text,now)
            output = f'Informação acrescentada ao chamado {ticket.code}.'
            session.state, session.draft = 'idle', {}
        else:
            output = 'Não foi possível acrescentar a informação. Envie o texto ou CANCELAR.'
    else:
        session.state, session.draft = 'offer', {}
        output = menu(contact)
    if output:
        reply(message, output, ticket)
    session.expires_at = None if session.state == 'human' else now + timedelta(hours=2)
    session.save()
    message.processed_at, message.error = now, ''
    message.save(update_fields=['processed_at', 'error'])


@transaction.atomic
def update_ticket(ticket_id, actor, *, status, text, internal, expected_revision, assigned_to=None, priority=None, category=None, start_sla=False, asset=None, problem=None, classify_links=False):
    snapshot = Ticket.objects.get(pk=ticket_id)
    Contact.objects.select_for_update().get(pk=snapshot.contact_id)
    ticket = Ticket.objects.select_for_update().select_related('contact', 'company').get(pk=ticket_id)
    if ticket.company_id not in company_ids(actor):
        raise PermissionDenied
    if ticket.revision != expected_revision:
        raise ValidationError('O chamado foi atualizado por outra pessoa. Atualize a página antes de salvar.')
    if status not in STATUS or (status != ticket.status and status not in TRANSITIONS[ticket.status]):
        raise ValidationError('Essa mudança de status não é permitida.')
    if assigned_to and (not assigned_to.is_active or not assigned_to.is_staff or
                       ticket.company_id not in company_ids(assigned_to)):
        raise ValidationError('O responsável não tem acesso a esta empresa.')
    if not text.strip():
        raise ValidationError('Descreva a atualização ou solução aplicada.')
    if internal and status != ticket.status:
        raise ValidationError('Mudanças de status são públicas. Use uma anotação separada para conteúdo interno.')
    if priority is not None and priority not in dict(ticket._meta.get_field('priority').choices):
        raise ValidationError('Prioridade inválida.')
    if category is not None and category not in dict(Ticket.CATEGORIES):
        raise ValidationError('Categoria inválida.')
    if classify_links:
        if asset and asset.company_id != ticket.company_id:
            raise ValidationError('O equipamento deve pertencer à empresa do chamado.')
        if problem and problem.company_id != ticket.company_id:
            raise ValidationError('A recorrência deve pertencer à empresa do chamado.')
        ticket.asset, ticket.problem = asset, problem
    previous_sla = {name: getattr(ticket, name).isoformat() if getattr(ticket, name) else None
                    for name in ('sla_started_at', 'response_due_at', 'resolution_due_at', 'first_responded_at', 'resolved_at')}
    previous_priority, previous_category = ticket.priority, ticket.category
    ticket.priority = priority or ticket.priority
    ticket.category = category or ticket.category
    previous = ticket.status
    ticket.status, ticket.assigned_to = status, assigned_to
    ticket.revision += 1
    now = timezone.now()
    reopened = status in OPEN and previous in ('resolved', 'closed', 'cancelled')
    if previous not in OPEN and not reopened and ticket.priority != previous_priority:
        raise ValidationError('Reabra o chamado antes de alterar a prioridade; os prazos concluídos são preservados.')
    if reopened or (start_sla and not ticket.sla_started_at):
        if status not in OPEN:
            raise ValidationError('Inicie o SLA apenas em um chamado em aberto.')
        if reopened:
            ticket.sla_cycle += 1
            ticket.reopen_count += 1
        initialize_sla(ticket, now)
    elif ticket.sla_started_at and ticket.priority != previous_priority:
        reprioritize(ticket)
    if not internal and ticket.sla_started_at and not ticket.first_responded_at:
        ticket.first_responded_at = now
    waiting_transition(ticket, previous, now)
    changed_resolution = status in ('resolved', 'closed') and previous not in ('resolved', 'closed')
    if changed_resolution:
        ticket.resolved_at = now
    elif status in OPEN:
        ticket.resolved_at = None
        Satisfaction.objects.filter(ticket=ticket, answered_at__isnull=True).update(cancelled=True)
    ticket.save()
    update = TicketUpdate.objects.create(ticket=ticket, author=actor.get_username(), text=text,
        internal=internal, old_status=previous, new_status=status)
    if not internal:
        body = f'*{ticket.code}* · {STATUS[status]}\n{text}'
        if changed_resolution:
            survey = Satisfaction.objects.create(ticket=ticket, cycle=ticket.revision, expires_at=now + timedelta(days=7))
            body += '\n\n' + survey_prompt(pending_surveys(ticket.contact).first() or survey)
            Conversation.objects.filter(contact=ticket.contact, company=ticket.company).update(
                state='idle', draft={}, expires_at=None)
        queue(ticket.contact, body, f'update:{update.pk}', ticket, ticket.company_id, ticket.requester_phone)
    audit(actor.get_username(), 'support_update', ticket, {'update': update.pk, 'from': previous,
          'to': status, 'internal': internal, 'assigned_to': assigned_to.pk if assigned_to else None,
          'priority_from': previous_priority, 'priority_to': ticket.priority,
          'category_from': previous_category, 'category_to': ticket.category,
          'sla_started_at': ticket.sla_started_at.isoformat() if ticket.sla_started_at else None,
          'response_due_at': ticket.response_due_at.isoformat() if ticket.response_due_at else None,
          'resolution_due_at': ticket.resolution_due_at.isoformat() if ticket.resolution_due_at else None,
          'sla_reopened': reopened, 'sla_before': previous_sla,
          'first_responded_at': ticket.first_responded_at.isoformat() if ticket.first_responded_at else None,
          'resolved_at': ticket.resolved_at.isoformat() if ticket.resolved_at else None,
          'sla_calendar': ticket.sla_calendar, 'sla_paused_at': ticket.sla_paused_at.isoformat() if ticket.sla_paused_at else None,
          'asset_id':ticket.asset_id, 'problem_id':ticket.problem_id})
    return ticket
