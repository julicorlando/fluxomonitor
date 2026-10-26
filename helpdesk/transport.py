"""WAHA adapter: signed ingress, durable inbox/outbox, no guessed telephone identity."""
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.error import HTTPError, URLError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction, models
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac
from flow.whatsapp import gateway, session_info
from .identity import phone_from_chat
from .models import BotConfig, Contact, DeliveryAck, Inbound, Outbound, VerifiedChatIdentity


def hook_key():
    return salted_hmac('gesti.support.webhook.v1', 'default', secret=settings.SECRET_KEY, algorithm='sha256').hexdigest()


def valid_signature(raw, signature, algorithm):
    expected = hmac.new(hook_key().encode(), raw, hashlib.sha512).hexdigest()
    return algorithm.lower() == 'sha512' and hmac.compare_digest(expected, signature.lower())


def ensure_webhook():
    cfg = BotConfig.objects.get(pk=1)
    if not cfg.enabled:
        return
    url = os.getenv('SUPPORT_WEBHOOK_URL', 'http://web:8000/support/webhook/')
    session = session_info()
    if session.get('status') != 'WORKING':
        raise ValidationError('Aguardando a conexão da sessão WhatsApp existente.')
    config = dict(session.get('config') or {})
    hooks = config.get('webhooks') or []
    # NOWEB requires its contact store to resolve WhatsApp LIDs to phone numbers.
    # Preserve engine options and existing data; never logout/delete the session.
    engine = session.get('engine') or os.getenv('WHATSAPP_DEFAULT_ENGINE', 'NOWEB')
    if isinstance(engine, dict):
        engine = engine.get('engine', 'NOWEB')
    store_ready = True
    if str(engine).upper() == 'NOWEB':
        noweb = dict(config.get('noweb') or {})
        store = dict(noweb.get('store') or {})
        store_ready = store.get('enabled') is True and store.get('fullSync') is True
        store.update({'enabled': True, 'fullSync': True})
        noweb['store'] = store
        config['noweb'] = noweb
    own = {'url': url, 'customHeaders': [{'name': 'Host', 'value': 'localhost'}], 'events': ['message', 'message.ack'], 'hmac': {'key': hook_key()},
           'retries': {'policy': 'exponential', 'delaySeconds': 2, 'attempts': 10}}
    existing = next((h for h in hooks if h.get('url') == url), None)
    matches = bool(existing and set(existing.get('events') or []) >= set(own['events'])
                   and existing.get('hmac', {}).get('key') == hook_key()
                   and all(header in (existing.get('customHeaders') or []) for header in own['customHeaders']))
    if not matches or not store_ready:
        config['webhooks'] = [h for h in hooks if h.get('url') != url] + [own]
        # Preserve all other session config and webhooks. PUT can briefly restart the session.
        gateway('PUT', '/api/sessions/default', {'name': 'default', 'config': config})
    logging.info('Recebimento do Suporte de TI verificado: sessão default, webhook autenticado.')
    BotConfig.objects.filter(pk=1).update(webhook_ready=True, webhook_checked_at=timezone.now(), detail='Recebimento configurado na sessão WhatsApp existente.')


def verified_destination(contact):
    """Resolve Brazilian WhatsApp IDs via the official WAHA number-check API."""
    from urllib.parse import urlencode
    number = contact.phone.lstrip('+')
    result = gateway('GET', '/api/contacts/check-exists?' + urlencode({'phone': number, 'session': 'default'}))
    if not isinstance(result, dict) or result.get('numberExists') is not True:
        raise ValidationError('WAHA não confirmou este número como conta WhatsApp.')
    chat = result.get('chatId')
    if not isinstance(chat, str) or not re.fullmatch(r'[0-9]+@(lid|c[.]us|s[.]whatsapp[.]net)', chat):
        raise ValidationError('WAHA retornou identidade de destino inválida.')
    # Only accept a PN matching the enrolled number; a verified LID is safe as a destination.
    pn = result.get('pn')
    if pn and phone_from_chat(pn) != contact.phone:
        raise ValidationError('WAHA retornou número diferente do cadastro.')
    VerifiedChatIdentity.objects.update_or_create(contact=contact, defaults={'phone': contact.phone, 'chat_id': chat})
    return chat


def resolve_phone(payload):
    sender = payload.get('from')
    phone = phone_from_chat(sender)
    if phone:
        return phone
    if not isinstance(sender, str) or not re.fullmatch(r'[0-9]+@lid', sender):
        return None
    # Only trusted gateway metadata, never a phone written in the user's message.
    data = payload.get('_data') if isinstance(payload.get('_data'), dict) else {}
    key = data.get('key') if isinstance(data.get('key'), dict) else {}
    # Phone identity may be carried by trusted WAHA metadata when the sender is a LID.
    for candidate in (key.get('remoteJidAlt'), data.get('remoteJidAlt'),
                      payload.get('participant'), data.get('participantPn')):
        phone = phone_from_chat(candidate)
        if phone:
            return phone
    mapped = gateway('GET', '/api/default/lids/' + sender)
    phone = phone_from_chat(mapped.get('pn')) if isinstance(mapped, dict) else None
    if not phone:
        identity = VerifiedChatIdentity.objects.filter(chat_id=sender, contact__active=True,
            phone=models.F('contact__phone')).select_related('contact').first()
        if identity:
            return identity.phone

        # Contact lookup uses a different NOWEB path than /lids and can expose
        # the verified PN held by Baileys even when /lids returns pn=null.
        # A contact name or freeform message is never sufficient for identity.
        from urllib.parse import urlencode
        lookup = gateway('GET', '/api/contacts?' + urlencode({
            'contactId': sender, 'session': 'default'}))
        if isinstance(lookup, dict):
            number = lookup.get('number')
            candidate = phone_from_chat(number + '@c.us') if isinstance(number, str) and number.isdecimal() else None
            if candidate and Contact.objects.filter(phone=candidate, active=True).exists():
                # Cache only a mapping supplied by WAHA contact resolution.
                matched = Contact.objects.get(phone=candidate, active=True)
                VerifiedChatIdentity.objects.update_or_create(contact=matched, defaults={
                    'phone': matched.phone, 'chat_id': sender})
                logging.info('Suporte WAHA: LID resolvido por consulta de contato, contact=%s', matched.pk)
                return candidate

        # NOWEB may omit the phone in /lids even when chatId resolves on WAHA.
        # Only accept an exact, unique, independently verified chatId match.
        from urllib.parse import urlencode
        matches = []
        for enrolled in Contact.objects.filter(active=True).only('id', 'phone').iterator():
            if not enrolled.phone.startswith('+55'):
                continue
            check = gateway('GET', '/api/contacts/check-exists?' + urlencode({
                'phone': enrolled.phone.lstrip('+'), 'session': 'default'}))
            if not isinstance(check, dict) or check.get('numberExists') is not True:
                continue
            if check.get('chatId') != sender:
                continue
            pn = check.get('pn')
            if pn and phone_from_chat(pn) != enrolled.phone:
                continue
            matches.append(enrolled)
            if len(matches) > 1:
                break
        if len(matches) == 1:
            enrolled = matches[0]
            VerifiedChatIdentity.objects.update_or_create(contact=enrolled, defaults={
                'phone': enrolled.phone, 'chat_id': sender})
            logging.info('Suporte WAHA: LID resolvido por check-exists, contact=%s', enrolled.pk)
            return enrolled.phone
        raise ValidationError('LID ainda não resolvido; aguardar sincronização e repetir webhook.')
    return phone


def receive_event(event):
    cfg = BotConfig.objects.get(pk=1)
    if not cfg.enabled or event.get('session') != 'default':
        return
    payload = event.get('payload')
    if not isinstance(payload, dict):
        return
    kind = event.get('event')
    if kind == 'message.ack':
        handle_ack(payload)
        return
    if kind != 'message' or payload.get('fromMe') is not False:
        return
    external = payload.get('id')
    if not isinstance(external, str) or not 1 <= len(external) <= 250:
        return
    if Inbound.objects.filter(external_id=external).exists():
        return
    # No bot session, inbox record, response or ticket for an unknown/inactive number.
    phone = resolve_phone(payload)
    contact = Contact.objects.filter(phone=phone, active=True).first() if phone else None
    if not contact:
        return
    try:
        stamp = float(payload.get('timestamp'))
        sent = datetime.fromtimestamp(stamp, tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return
    now = timezone.now()
    if sent < now - timedelta(hours=24) or sent > now + timedelta(minutes=5):
        return
    text = payload.get('body') or ''
    if not isinstance(text, str) or len(text) > 4000:
        text = ''  # prompts validation instead of silently truncating the problem
    if Inbound.objects.filter(contact=contact, received_at__gt=now - timedelta(minutes=1)).count() >= 30:
        return
    Inbound.objects.get_or_create(external_id=external, defaults={
        'contact': contact, 'company': contact.company, 'phone': phone, 'text': text, 'sent_at': sent})
    BotConfig.objects.filter(pk=1).update(last_webhook_at=now)


def handle_ack(payload):
    external, phone = payload.get('id'), phone_from_chat(payload.get('to'))
    rank = payload.get('ack')
    if not isinstance(external, str) or len(external) > 250 or not phone or type(rank) is not int or rank not in (-1, 0, 1, 2, 3, 4):
        return
    # Restrict receipts to destinations known to this module; they never enter the bot.
    if not Contact.objects.filter(phone=phone, active=True).exists():
        return
    with transaction.atomic():
        ack, _ = DeliveryAck.objects.get_or_create(external_id=external, defaults={'phone': phone, 'rank': rank})
        ack = DeliveryAck.objects.select_for_update().get(pk=ack.pk)
        if ack.phone != phone:
            return
        if rank > ack.rank:
            ack.rank = rank
            ack.save(update_fields=['rank'])
        apply_ack(external, phone, ack.rank)


def apply_ack(external, phone, rank):
    with transaction.atomic():
        for item in Outbound.objects.select_for_update().filter(gateway_id=external, phone=phone).exclude(status='blocked'):
            old = {'unknown': 0, 'failed': 0, 'accepted': 1, 'delivered': 2, 'read': 3}.get(item.status, 0)
            new = 3 if rank >= 3 else rank
            if new > old:
                item.status = {1: 'accepted', 2: 'delivered', 3: 'read'}[new]
                item.detail = 'Estado confirmado pelo webhook do gateway.'
                item.save(update_fields=['status', 'detail'])
            elif rank == -1 and old < 2:
                item.status, item.detail = 'failed', 'Falha informada pelo gateway.'
                item.save(update_fields=['status', 'detail'])


def dispatch(limit=5):
    now = timezone.now()
    Outbound.objects.filter(status='sending', attempted_at__lt=now - timedelta(minutes=2)).update(
        status='unknown', detail='Processamento interrompido; confira o destinatário antes de repetir.')
    Outbound.objects.filter(status='pending', created_at__lt=now - timedelta(hours=24)).update(
        status='blocked', detail='Mensagem expirou após 24 horas sem envio.')
    if not BotConfig.objects.filter(pk=1, enabled=True).exists():
        return
    pending = Outbound.objects.filter(status='pending').filter(
        Q(next_attempt_at__isnull=True) |
        Q(next_attempt_at__lte=now))
    ids = list(pending.order_by('id').values_list('pk', flat=True)[:limit])
    if not ids:
        return
    if session_info().get('status') != 'WORKING':
        Outbound.objects.filter(pk__in=ids, status='pending').update(detail='Aguardando conexão do WhatsApp.')
        return
    for pk in ids:
        with transaction.atomic():
            snapshot = Outbound.objects.get(pk=pk)
            contact = Contact.objects.select_for_update().get(pk=snapshot.contact_id)
            item = Outbound.objects.select_for_update().get(pk=pk)
            if item.status != 'pending':
                continue
            if (not contact.active or contact.phone != item.phone or contact.company_id != item.company_id or
                    not BotConfig.objects.filter(pk=1, enabled=True).exists()):
                item.status, item.detail = 'blocked', 'Destinatário inativo, cadastro alterado ou bot pausado.'
                item.save(update_fields=['status', 'detail'])
                continue
            # Keep each contact's responses in FIFO order while another worker is sending.
            if Outbound.objects.filter(contact=contact, pk__lt=pk, status__in=('pending', 'sending')).exists():
                continue
            item.status, item.attempted_at = 'sending', now
            item.attempts += 1
            item.save(update_fields=['status', 'attempted_at', 'attempts'])
        status, detail, external = 'unknown', 'Resultado incerto; sem repetição automática para evitar duplicidade.', ''
        retry = False
        try:
            chat_id = verified_destination(contact)
            result = gateway('POST', '/api/sendText', {'session': 'default', 'chatId': chat_id, 'text': item.text})
            external = result.get('id', '') if isinstance(result, dict) else ''
            if isinstance(external, dict):
                external = external.get('_serialized', '')
            external = str(external)[:250]
            if external:
                status, detail = 'accepted', 'Aceito pelo gateway; aguardando confirmação de entrega.'
            else:
                detail = 'Gateway respondeu sem ID de mensagem; conferir API WAHA e registros do serviço support.'
                logging.error('Suporte WAHA: sendText respondeu sem identificador, tipo=%s, outbound=%s', type(result).__name__, pk)
        except ValidationError as exc:
            status = 'failed'
            detail = 'Número não confirmado no WAHA; envio não executado.'
            logging.warning('Suporte WAHA: número não confirmado, outbound=%s', pk)
        except HTTPError as exc:
            if exc.code == 429:
                retry = True
                detail = 'Limite temporário do gateway; aguardando nova tentativa.'
            elif 400 <= exc.code < 500:
                status, detail = 'failed', f'Envio recusado pelo gateway (HTTP {exc.code}).'
            else:
                detail = f'Falha HTTP {exc.code} do gateway; resultado incerto.'
            logging.error('Suporte WAHA: sendText HTTP %s, outbound=%s', exc.code, pk)
        except URLError as exc:
            # Only connection refusal/name resolution conclusively precede sending a request.
            import socket
            if isinstance(exc.reason, (ConnectionRefusedError, socket.gaierror)):
                retry = True
                detail = 'Gateway indisponível antes do envio; aguardando nova tentativa.'
        except Exception as exc:
            logging.exception('Suporte WAHA: erro em sendText, tipo=%s, outbound=%s', type(exc).__name__, pk)
            detail = f'Erro {type(exc).__name__} durante envio WAHA; resultado incerto.'
        if retry:
            status = 'pending' if item.attempts < 5 else 'failed'
        Outbound.objects.filter(pk=pk, status='sending').update(status=status, detail=detail,
            gateway_id=external, next_attempt_at=now + timedelta(seconds=min(300, 10 * 2 ** item.attempts)) if retry else None)
        if external:
            ack = DeliveryAck.objects.filter(external_id=external, phone=item.phone).first()
            if ack:
                apply_ack(external, item.phone, ack.rank)
        if status in ('failed', 'unknown'):
            from flow.notifications import notice
            notice(f'support-send:{pk}', 'Falha no atendimento WhatsApp',
                   f'Mensagem de suporte #{pk}: {detail} Confira Suporte de TI → Mensagens.')
