"""Persistent sender authorization. Sending messages must never overwrite these links."""
import re
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from .models import BotConfig, Contact, Inbound, SenderIdentity, UnresolvedInbound


def canonical_chat(chat):
    if not isinstance(chat, str) or not re.fullmatch(r'[0-9]+@(lid|c\.us|s\.whatsapp\.net)', chat) or len(chat) > 80:
        return None
    return chat.replace('@s.whatsapp.net', '@c.us')


def enrolled_binding(chat):
    chat = canonical_chat(chat)
    return SenderIdentity.objects.select_related('contact').filter(chat_id=chat).first() if chat else None


def usable(binding):
    return bool(binding and binding.contact.active and binding.phone == binding.contact.phone)


@transaction.atomic
def remember_sender(contact, chat, source='gateway', actor=''):
    chat = canonical_chat(chat)
    if not chat or source not in ('gateway', 'master', 'legacy'):
        raise ValidationError('Identificador de WhatsApp inválido.')
    contact = Contact.objects.select_for_update().get(pk=contact.pk)
    if not contact.active:
        raise ValidationError('Colaborador inativo.')
    binding, created = SenderIdentity.objects.get_or_create(chat_id=chat, defaults={
        'contact': contact, 'phone': contact.phone, 'source': source, 'confirmed_by': actor[:150]})
    binding = SenderIdentity.objects.select_for_update().get(pk=binding.pk)
    if binding.contact_id != contact.pk:
        raise ValidationError('Este identificador já está vinculado a outro colaborador.')
    if binding.phone != contact.phone:
        if source != 'master':
            raise ValidationError('O telefone cadastrado mudou; o vínculo precisa ser confirmado novamente.')
        binding.phone = contact.phone
    if source == 'master':
        binding.source, binding.confirmed_by = source, actor[:150]
    # A gateway refresh cannot downgrade a human-confirmed link or replace its ID.
    if not created and (source == 'master' or binding.phone != contact.phone):
        binding.save(update_fields=['phone', 'source', 'confirmed_by'])
    return binding


@transaction.atomic
def import_pending(binding, limit=100):
    contact = Contact.objects.select_for_update().get(pk=binding.contact_id)
    binding = SenderIdentity.objects.select_for_update().get(pk=binding.pk)
    if not contact.active or binding.phone != contact.phone:
        return 0
    now = timezone.now()
    pending = UnresolvedInbound.objects.select_for_update().filter(
        sender=binding.chat_id, status='unresolved', sent_at__gte=now - timedelta(hours=24),
        sent_at__lte=now + timedelta(minutes=5)).order_by('sent_at', 'id')[:limit]
    imported = 0
    for event in pending:
        message, created = Inbound.objects.get_or_create(external_id=event.external_id, defaults={
            'contact': contact, 'company': contact.company, 'phone': contact.phone,
            'text': event.text, 'sent_at': event.sent_at})
        if (message.contact_id, message.company_id, message.phone) != (contact.pk, contact.company_id, contact.phone):
            # Never relabel an existing message belonging to another person/company.
            continue
        event.status = 'linked'
        event.save(update_fields=['status', 'last_seen_at'])
        imported += int(created)
    return imported


def recover_bound_pending():
    if not BotConfig.objects.filter(pk=1, enabled=True).exists():
        return
    senders = UnresolvedInbound.objects.filter(status='unresolved', sent_at__gte=timezone.now() - timedelta(hours=24)).values_list('sender', flat=True)
    for binding in SenderIdentity.objects.filter(chat_id__in=senders, contact__active=True,
                                                 phone=F('contact__phone')).order_by('pk')[:50]:
        import_pending(binding)
