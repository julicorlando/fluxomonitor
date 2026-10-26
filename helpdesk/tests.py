import hashlib
import hmac
import json
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from flow.models import AuditLog, WhatsAppDelivery
from .forms import ContactForm
from .models import (AgentGrant, BotConfig, Company, Contact, Conversation, DeliveryAck,
                     Inbound, Outbound, Satisfaction, Ticket, TicketUpdate)
from .services import process_message, update_ticket
from .transport import dispatch, ensure_webhook, hook_key, receive_event


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class HelpdeskTests(TestCase):
    def setUp(self):
        self.pdu = Company.objects.get(code='PDU')
        self.cm = Company.objects.get(code='CM')
        self.contact = Contact.objects.create(company=self.pdu, name='Ana', phone='+5581999991234', email='ana@example.com')
        self.other = Contact.objects.create(company=self.cm, name='Bia', phone='+5581999995678', email='bia@example.com')
        self.root = get_user_model().objects.create_user('root', password='test-pass-123', is_staff=True, is_superuser=True)
        self.agent = get_user_model().objects.create_user('agent', password='test-pass-123', is_staff=True)
        AgentGrant.objects.create(user=self.agent, company=self.pdu)
        self.client.force_login(self.root)
        self.index = 0

    def payload(self, body='Olá', **kwargs):
        self.index += 1
        payload = {'id': f'false_5581999991234@c.us_MSG{self.index}', 'from': '5581999991234@c.us',
                   'fromMe': False, 'body': body, 'timestamp': int(timezone.now().timestamp())}
        payload.update(kwargs)
        return {'event': 'message', 'session': 'default', 'payload': payload}

    def say(self, text):
        event = self.payload(text)
        receive_event(event)
        message = Inbound.objects.get(external_id=event['payload']['id'])
        process_message(message.pk)
        return message

    def ticket(self):
        for text in ['Olá', 'Sim', 'Financeiro FIN-03', 'O computador não conecta à rede', 'Confirmar']:
            self.say(text)
        return Ticket.objects.latest('pk')

    def update(self, ticket, **kwargs):
        return update_ticket(ticket.pk, self.root, status=kwargs.get('status', ticket.status),
            text=kwargs.get('text', 'Atualização de teste'), internal=kwargs.get('internal', False),
            expected_revision=kwargs.get('revision', ticket.revision), assigned_to=kwargs.get('assigned_to'))

    def signed_post(self, event, **headers):
        raw = json.dumps(event).encode()
        sign = hmac.new(hook_key().encode(), raw, hashlib.sha512).hexdigest()
        return self.client.post('/support/webhook/', raw, content_type='application/json',
            HTTP_X_WEBHOOK_HMAC=headers.get('sign', sign), HTTP_X_WEBHOOK_HMAC_ALGORITHM='sha512')

    def test_seed_exactly_seven_companies(self):
        self.assertEqual(set(Company.objects.values_list('code', flat=True)), {'PDU','CM','SC','EVPI','PRED','FRT','TSP'})

    def test_contacts_normalize_and_validate(self):
        data = {'company': self.pdu.pk, 'name': 'Carlos', 'phone': '(81) 98888-1122', 'email': 'c@example.com', 'active': True}
        form = ContactForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().phone, '+5581988881122')
        for value in ['+55 11 98888-1122', '+1 81988881122', '819123', '+55 81 88888-1234']:
            form = ContactForm({**data, 'phone': value})
            self.assertFalse(form.is_valid())
        form = ContactForm({**data, 'phone': '+55 81 99999-1234'})
        self.assertFalse(form.is_valid())

    def test_unknown_inactive_groups_and_own_messages_silent(self):
        for params in [{'from': '5581988880000@c.us'}, {'from': '5581999995678@c.us'},
                       {'from': '12345@g.us'}, {'fromMe': True}, {'from': 'status@broadcast'}]:
            self.other.active = False
            self.other.save()
            self.assertEqual(self.signed_post(self.payload(**params)).status_code, 204)
        self.assertFalse(Inbound.objects.exists())
        self.assertFalse(Conversation.objects.exists())
        self.assertFalse(Outbound.objects.exists())
        self.assertFalse(Ticket.objects.exists())

    def test_bad_signature_does_not_write(self):
        self.assertEqual(self.signed_post(self.payload(), sign='bad').status_code, 403)
        self.assertFalse(Inbound.objects.exists())
        self.assertEqual(self.client.get('/support/webhook/').status_code, 405)

    def test_signed_webhook_persists_before_processing_and_deduplicates(self):
        event = self.payload()
        self.assertEqual(self.signed_post(event).status_code, 204)
        self.assertFalse(Outbound.objects.exists())
        self.signed_post(event)
        self.assertEqual(Inbound.objects.count(), 1)
        msg = Inbound.objects.get()
        process_message(msg.pk)
        process_message(msg.pk)
        self.assertEqual(Outbound.objects.count(), 1)

    def test_ticket_flow_snapshot_and_idempotence(self):
        ticket = self.ticket()
        self.assertEqual(ticket.code, 'PDU-00001')
        self.assertEqual(ticket.requester_phone, self.contact.phone)
        self.assertEqual(ticket.requester_email, self.contact.email)
        self.assertIn('aberto com sucesso', Outbound.objects.last().text)
        self.say('Confirmar')  # a second confirmation cannot create another ticket
        self.assertEqual(Ticket.objects.count(), 1)
        self.contact.company = self.cm
        self.contact.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.company, self.pdu)
        for log in AuditLog.objects.filter(action__startswith='support_'):
            log.full_clean()

    def test_numbering_independent_by_company(self):
        self.ticket()
        self.contact.company = self.cm
        self.contact.save()
        self.assertEqual(self.ticket().code, 'CM-00001')

    def test_no_answer_and_cancel_do_not_open_ticket(self):
        self.say('Oi')
        self.say('Não')
        self.say('Oi')
        self.say('Sim')
        self.say('Cancelar')
        self.assertEqual(Ticket.objects.count(), 0)

    def test_correction_replaces_draft(self):
        for text in ['Oi', 'Sim', 'FIN', 'Sem internet', '2', 'RH', 'Impressora não imprime', '1']:
            self.say(text)
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.target, 'RH')
        self.assertEqual(ticket.description, 'Impressora não imprime')

    def test_media_description_does_not_advance(self):
        for text in ['Oi','Sim','FIN','']:
            self.say(text)
        self.assertEqual(Conversation.objects.get().state, 'description')
        self.assertIn('por escrito', Outbound.objects.last().text)

    def test_lookup_and_note_are_requester_scoped(self):
        ticket = self.ticket()
        self.say('Oi'); self.say('2'); self.say('CM-00001')
        self.assertIn('Não encontrei', Outbound.objects.last().text)
        self.say(ticket.code)
        self.assertIn('Aberto', Outbound.objects.last().text)
        self.say('Oi'); self.say('3'); self.say(ticket.code); self.say('Afeta dois computadores')
        self.assertEqual(ticket.updates.last().text, 'Afeta dois computadores')

    def test_human_handoff_and_silent_followups(self):
        self.say('Atendente')
        ticket = Ticket.objects.get()
        self.assertEqual(Conversation.objects.get().state, 'human')
        before = Outbound.objects.count()
        self.say('Preciso de atendimento urgente')
        self.assertEqual(Outbound.objects.count(), before)
        self.assertEqual(ticket.updates.last().text, 'Preciso de atendimento urgente')

    def test_expired_draft_restarts(self):
        self.say('Oi'); self.say('Sim')
        Conversation.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.say('Financeiro')
        self.assertEqual(Conversation.objects.get().state, 'offer')

    def test_out_of_order_input_cannot_advance(self):
        self.say('Oi')
        event = self.payload('Sim', timestamp=int((timezone.now()-timedelta(minutes=2)).timestamp()))
        receive_event(event)
        process_message(Inbound.objects.latest('id').pk)
        self.assertEqual(Conversation.objects.get().state, 'offer')

    def test_old_or_future_webhooks_ignored(self):
        for stamp in [timezone.now()-timedelta(days=2), timezone.now()+timedelta(hours=1)]:
            receive_event(self.payload(timestamp=stamp.timestamp()))
        self.assertFalse(Inbound.objects.exists())

    def test_public_updates_and_internal_notes(self):
        ticket = self.ticket()
        count = Outbound.objects.count()
        ticket = self.update(ticket, text='Segredo interno', internal=True)
        self.assertEqual(Outbound.objects.count(), count)
        ticket = self.update(ticket, status='working', text='Estamos verificando')
        self.assertEqual(Outbound.objects.count(), count+1)
        self.assertNotIn('Segredo', Outbound.objects.last().text)
        self.assertEqual(Outbound.objects.last().phone, ticket.requester_phone)

    def test_optimistic_conflict_and_no_internal_status_change(self):
        ticket = self.ticket()
        self.update(ticket)
        with self.assertRaises(ValidationError):
            self.update(ticket, revision=1)
        ticket.refresh_from_db()
        with self.assertRaises(ValidationError):
            self.update(ticket, status='resolved', internal=True)

    def test_company_permissions_enforced_in_service_and_views(self):
        ticket = self.ticket()
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(f'/support/tickets/{ticket.pk}/').status_code, 200)
        ticket.company = self.cm
        ticket.save()
        self.assertEqual(self.client.get(f'/support/tickets/{ticket.pk}/').status_code, 404)
        self.assertEqual(self.client.get(f'/support/pulse/?ticket={ticket.pk}').status_code, 404)
        with self.assertRaises(PermissionDenied):
            update_ticket(ticket.pk, self.agent, status='working', text='x', internal=False, expected_revision=1)
        self.assertNotContains(self.client.get('/support/'), ticket.code)
        self.assertEqual(self.client.get('/support/contacts/').status_code, 302)

    def test_resolution_evaluation_and_feedback(self):
        ticket = self.update(self.ticket(), status='resolved', text='Rede restabelecida')
        self.assertIn(f'AVALIAR {ticket.code} 5', Outbound.objects.last().text)
        self.say(f'AVALIAR {ticket.code} 4')
        survey = Satisfaction.objects.get()
        self.assertEqual(survey.rating, 4)
        self.say(f'FEEDBACK {ticket.code} Atendimento rápido')
        survey.refresh_from_db()
        self.assertEqual(survey.feedback, 'Atendimento rápido')
        before = Satisfaction.objects.count()
        self.update(ticket, status='closed')
        self.assertEqual(Satisfaction.objects.count(), before)

    def test_duplicate_ratings_do_not_overwrite_and_reopen_cancels_pending(self):
        ticket = self.update(self.ticket(), status='resolved')
        self.say(f'AVALIAR {ticket.code} 4')
        self.say(f'AVALIAR {ticket.code} 1')
        self.assertEqual(Satisfaction.objects.get().rating, 4)
        ticket = self.update(ticket, status='working')
        ticket = self.update(ticket, status='resolved')
        self.update(ticket, status='working')
        self.assertTrue(Satisfaction.objects.latest('pk').cancelled)

    def test_resolution_does_not_discard_another_draft(self):
        ticket = self.ticket()
        self.say('Oi'); self.say('1'); self.say('RH')
        self.update(ticket, status='resolved')
        self.say('Outro problema no computador')
        self.assertEqual(Conversation.objects.get().state, 'confirm')

    def test_pages_render_and_csrf_is_required(self):
        ticket = self.ticket()
        for url in ['/support/','/support/contacts/','/support/settings/','/support/messages/',f'/support/tickets/{ticket.pk}/']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post('/support/settings/', {'action':'toggle','enabled':'0'}).status_code, 403)
        self.assertEqual(client.post(f'/support/tickets/{ticket.pk}/', {}).status_code, 403)

    @patch('helpdesk.transport.session_info', return_value={'status':'WORKING'})
    @patch('helpdesk.transport.gateway')
    def test_dispatch_and_ack_is_monotonic(self, gateway, info):
        gateway.side_effect = lambda method, path, *args: ({'numberExists':True,'chatId':self.contact.phone[1:]+'@c.us','pn':self.contact.phone[1:]+'@c.us'} if method == 'GET' else {'id':'WA-123'})
        self.say('Oi')
        dispatch()
        item = Outbound.objects.get()
        self.assertEqual(item.status, 'accepted')
        dispatch()
        self.assertEqual(gateway.call_count, 2)
        for rank in [3,2,1,-1]:
            receive_event({'session':'default','event':'message.ack','payload':{'id':'WA-123','to':self.contact.phone[1:]+'@c.us','ack':rank}})
        item.refresh_from_db()
        self.assertEqual(item.status, 'read')

    @patch('helpdesk.transport.session_info', return_value={'status':'WORKING'})
    @patch('helpdesk.transport.gateway', side_effect=TimeoutError())
    def test_uncertain_send_never_retried(self, gateway, info):
        self.say('Oi')
        dispatch(); dispatch()
        self.assertEqual(Outbound.objects.get().status, 'unknown')
        self.assertEqual(gateway.call_count, 1)

    @patch('helpdesk.transport.session_info', return_value={'status':'WORKING'})
    @patch('helpdesk.transport.gateway', side_effect=URLError(ConnectionRefusedError()))
    def test_definite_connection_failure_retried_with_backoff(self, gateway, info):
        self.say('Oi')
        dispatch(); dispatch()
        item = Outbound.objects.get()
        self.assertEqual(item.status, 'pending')
        self.assertGreater(item.next_attempt_at, timezone.now())
        self.assertEqual(gateway.call_count, 1)

    @patch('helpdesk.transport.session_info', return_value={'status':'WORKING'})
    @patch('helpdesk.transport.gateway')
    def test_deactivated_contact_blocks_pending(self, gateway, info):
        self.say('Oi')
        self.contact.active = False
        self.contact.save()
        dispatch()
        self.assertEqual(Outbound.objects.get().status, 'blocked')
        gateway.assert_not_called()

    @patch('helpdesk.transport.gateway')
    def test_lid_resolves_only_through_trusted_gateway(self, gateway):
        gateway.return_value = {'lid':'1234@lid','pn':self.contact.phone[1:]+'@c.us'}
        self.signed_post(self.payload(**{'from':'1234@lid'}))
        self.assertEqual(Inbound.objects.get().contact, self.contact)
        gateway.assert_called_once_with('GET', '/api/default/lids/1234@lid')

    @patch('helpdesk.transport.gateway')
    @patch('helpdesk.transport.session_info')
    def test_configure_webhook_preserves_session_and_other_hooks(self, info, gateway):
        original = {'url':'https://other.example/hook','events':['session.status']}
        info.return_value = {'status':'WORKING','config':{'metadata':{'owner':'TI'},'webhooks':[original]}}
        ensure_webhook()
        body = gateway.call_args.args[2]
        self.assertEqual(body['config']['noweb']['store'], {'enabled': True, 'fullSync': True})
        self.assertEqual(body['config']['metadata'], {'owner':'TI'})
        self.assertIn(original, body['config']['webhooks'])
        self.assertEqual(body['config']['webhooks'][-1]['hmac']['key'], hook_key())
        info.return_value = {'status':'WORKING','config':body['config']}
        ensure_webhook()
        self.assertEqual(gateway.call_count, 1)

    def test_paused_bot_is_silent_and_pending_not_processed(self):
        receive_event(self.payload())
        BotConfig.objects.update(enabled=False)
        process_message(Inbound.objects.get().pk)
        self.signed_post(self.payload())
        self.assertEqual(Inbound.objects.count(), 1)
        self.assertFalse(Outbound.objects.exists())

    def test_whatsapp_flow_delivery_untouched(self):
        self.ticket()
        self.assertFalse(WhatsAppDelivery.objects.exists())

    def test_real_form_creates_contact_and_only_superadmin_can_edit(self):
        result = self.client.post('/support/contacts/', {'company':self.cm.pk,'name':'Novo cadastro',
            'phone':'+55 (81) 98888-7788','email':'novo@example.com','active':'on'})
        self.assertEqual(result.status_code, 302)
        contact = Contact.objects.get(phone='+5581988887788')
        self.assertEqual(contact.company, self.cm)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(f'/support/contacts/{contact.pk}/', {}).status_code, 302)

    def test_update_form_really_enqueues_and_uses_revision(self):
        ticket = self.ticket()
        result = self.client.post(f'/support/tickets/{ticket.pk}/', {'status':'working','text':'Teste técnico em andamento',
            'revision':ticket.revision, 'assigned_to':self.root.pk})
        self.assertEqual(result.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'working')
        self.assertEqual(ticket.assigned_to, self.root)
        self.assertIn('Teste técnico', Outbound.objects.last().text)
        before = Outbound.objects.count()
        result = self.client.post(f'/support/tickets/{ticket.pk}/', {'status':'resolved','text':'Resolvido', 'revision':1})
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, 'atualizado por outra pessoa')
        self.assertEqual(Outbound.objects.count(), before)

    def test_multiple_surveys_require_protocol(self):
        first = self.update(self.ticket(), status='resolved')
        second = self.update(self.ticket(), status='resolved')
        self.say('5')
        self.assertIn('mais de uma avaliação', Outbound.objects.last().text)
        self.assertFalse(Satisfaction.objects.filter(rating__isnull=False).exists())
        self.say(f'AVALIAR {first.code} 5')
        self.assertEqual(Satisfaction.objects.get(ticket=first).rating, 5)
        self.assertIsNone(Satisfaction.objects.get(ticket=second).rating)

    def test_unauthorized_assignee_rejected(self):
        user = get_user_model().objects.create_user('other', is_staff=True)
        AgentGrant.objects.create(user=user, company=self.cm)
        with self.assertRaises(ValidationError):
            self.update(self.ticket(), assigned_to=user)

    @patch('helpdesk.transport.session_info', return_value={'status':'WORKING'})
    @patch('helpdesk.transport.gateway')
    def test_ack_before_send_response_is_reconciled(self, gateway, info):
        gateway.side_effect = lambda method, path, *args: ({'numberExists':True, 'chatId':self.contact.phone[1:]+'@c.us'} if method == 'GET' else {'id':'WA-EARLY'})
        self.say('Oi')
        receive_event({'session':'default','event':'message.ack','payload':{'id':'WA-EARLY','to':self.contact.phone[1:]+'@c.us','ack':2}})
        dispatch()
        self.assertEqual(Outbound.objects.get().status, 'delivered')

    @patch('helpdesk.transport.session_info', return_value={'status':'WORKING'})
    @patch('helpdesk.transport.gateway')
    def test_interrupted_send_and_expired_pending_not_retried(self, gateway, info):
        self.say('Oi')
        Outbound.objects.update(status='sending', attempted_at=timezone.now()-timedelta(minutes=5))
        dispatch()
        self.assertEqual(Outbound.objects.get().status, 'unknown')
        gateway.assert_not_called()
        Outbound.objects.update(status='pending', created_at=timezone.now()-timedelta(days=2))
        dispatch()
        self.assertEqual(Outbound.objects.get().status, 'blocked')
        gateway.assert_not_called()

    def test_grants_can_be_replaced_without_widening_other_users(self):
        response = self.client.post('/support/settings/', {'action':'grant','user':self.agent.pk,'companies':[self.cm.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(AgentGrant.objects.filter(user=self.agent).values_list('company_id',flat=True)), [self.cm.pk])
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post('/support/settings/', {'action':'grant','user':self.agent.pk,'companies':[self.pdu.pk]}).status_code, 302)
        self.assertEqual(list(AgentGrant.objects.filter(user=self.agent).values_list('company_id',flat=True)), [self.cm.pk])

    def test_scoped_json_api_and_atomic_updates(self):
        ticket = self.ticket()
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get('/api/support/tickets/').json()['count'], 1)
        response = self.client.post(f'/api/support/tickets/{ticket.pk}/updates/',
            json.dumps({'status':'working','text':'Investigando pela API','revision':ticket.revision}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'working')
        self.assertEqual(self.client.get(f'/api/support/tickets/{ticket.pk}/').json()['code'], ticket.code)
        ticket.company = self.cm
        ticket.save()
        self.assertEqual(self.client.get('/api/support/tickets/').json()['count'], 0)
        self.assertEqual(self.client.get(f'/api/support/tickets/{ticket.pk}/').status_code, 404)
        self.assertEqual(self.client.post(f'/api/support/tickets/{ticket.pk}/updates/', '{}', content_type='application/json').status_code, 404)

    def test_api_write_requires_csrf(self):
        ticket = self.ticket()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post(f'/api/support/tickets/{ticket.pk}/updates/', '{}', content_type='application/json').status_code, 403)

    @patch('helpdesk.transport.gateway')
    @patch('helpdesk.transport.session_info')
    def test_existing_hook_still_enables_noweb_store_without_erasing_options(self, info, gateway):
        info.return_value = {'status': 'WORKING', 'engine': {'engine': 'NOWEB'}, 'config': {'noweb': {'markOnline': False, 'store': {'enabled': False, 'fullSync': False, 'custom': 'keep'}}}}
        ensure_webhook()
        config = gateway.call_args.args[2]['config']
        self.assertEqual(config['noweb']['store'], {'enabled': True, 'fullSync': True, 'custom': 'keep'})
        self.assertFalse(config['noweb']['markOnline'])
        gateway.reset_mock()
        config['noweb']['store']['enabled'] = False
        info.return_value = {'status': 'WORKING', 'engine': 'NOWEB', 'config': config}
        ensure_webhook()
        self.assertEqual(gateway.call_count, 1)
        self.assertTrue(gateway.call_args.args[2]['config']['noweb']['store']['enabled'])

    @patch('helpdesk.transport.gateway')
    @patch('helpdesk.transport.session_info')
    def test_other_engines_do_not_receive_noweb_store_changes(self, info, gateway):
        info.return_value = {'status': 'WORKING', 'engine': 'WEBJS', 'config': {}}
        ensure_webhook()
        self.assertNotIn('noweb', gateway.call_args.args[2]['config'])


    def test_master_inbox_private_and_manual_reply_is_queued(self):
        self.say('Oi')
        page = self.client.get('/support/inbox/')
        self.assertEqual(page.status_code, 200)
        page = self.client.get(f'/support/inbox/{self.contact.pk}/')
        self.assertContains(page, 'Oi')
        response = self.client.post(f'/support/inbox/{self.contact.pk}/', {'text': 'Recebido, vou verificar.'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Outbound.objects.filter(key__startswith='master:').count(), 1)
        self.assertEqual(Outbound.objects.filter(key__startswith='master:').first().status, 'pending')
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get('/support/inbox/').status_code, 302)
        self.assertEqual(self.client.post(f'/support/inbox/{self.contact.pk}/', {'text': 'Não autorizado'}).status_code, 302)
        self.assertEqual(Outbound.objects.filter(key__startswith='master:').count(), 1)

    def test_master_inbox_never_sends_to_inactive_contact(self):
        self.contact.active = False
        self.contact.save(update_fields=['active'])
        self.assertEqual(self.client.get(f'/support/inbox/{self.contact.pk}/').status_code, 404)
        self.assertEqual(self.client.post(f'/support/inbox/{self.contact.pk}/', {'text':'Olá'}).status_code, 404)
        self.assertFalse(Outbound.objects.filter(key__startswith='master:').exists())

    @patch('helpdesk.transport.gateway', return_value={'lid': '1234@lid', 'pn': None})
    def test_unresolved_lid_returns_retryable_webhook_error(self, gateway):
        response = self.signed_post(self.payload(**{'from': '1234@lid'}))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(Inbound.objects.exists())
        self.assertFalse(Outbound.objects.exists())

    @patch('helpdesk.transport.session_info', return_value={'status': 'WORKING'})
    @patch('helpdesk.transport.gateway')
    def test_gateway_without_message_id_records_specific_diagnostic(self, gateway, info):
        gateway.side_effect = lambda method, path, *args: ({'numberExists':True, 'chatId':self.contact.phone[1:]+'@c.us'} if method == 'GET' else {})
        self.say('Oi')
        dispatch()
        item = Outbound.objects.get()
        self.assertEqual(item.status, 'unknown')
        self.assertIn('sem ID', item.detail)

    @patch('helpdesk.transport.session_info', return_value={'status': 'WORKING'})
    @patch('helpdesk.transport.gateway', side_effect=HTTPError('http://waha', 500, 'error', {}, None))
    def test_gateway_http_500_records_status_without_retry(self, gateway, info):
        self.say('Oi')
        dispatch()
        item = Outbound.objects.get()
        self.assertEqual(item.status, 'unknown')
        self.assertIn('HTTP 500', item.detail)

    @patch('helpdesk.transport.session_info', return_value={'status': 'WORKING'})
    @patch('helpdesk.transport.gateway')
    def test_check_exists_verified_lid_routes_outbound_and_inbound(self, gateway, info):
        lid = '262453002793117@lid'
        gateway.side_effect = lambda method, path, *args: (
            {'numberExists': True, 'chatId': lid, 'pn': self.contact.phone[1:] + '@c.us'}
            if method == 'GET' and 'check-exists' in path else {'id': 'WA-lid'})
        self.say('Oi')
        dispatch()
        self.assertEqual(Outbound.objects.last().status, 'accepted')
        self.assertEqual(gateway.call_args.args[2]['chatId'], lid)
        incoming = self.payload('Novo pedido', **{'from': lid})
        receive_event(incoming)
        self.assertEqual(Inbound.objects.filter(external_id=incoming['payload']['id']).count(), 1)

    @patch('helpdesk.transport.session_info', return_value={'status': 'WORKING'})
    @patch('helpdesk.transport.gateway', return_value={'numberExists': False})
    def test_unregistered_whatsapp_number_is_not_sent_to(self, gateway, info):
        self.say('Oi')
        dispatch()
        self.assertEqual(Outbound.objects.get().status, 'failed')
        self.assertEqual(gateway.call_count, 1)

    def test_master_restarts_human_handoff_without_creating_duplicate_ticket(self):
        self.say('Atendente')
        ticket = Ticket.objects.get()
        self.assertEqual(Conversation.objects.get().state, 'human')
        before = Outbound.objects.count()
        response = self.client.post(f'/support/inbox/{self.contact.pk}/', {'action': 'restart_bot'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Conversation.objects.get().state, 'offer')
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Outbound.objects.count(), before + 1)
        self.assertIn('1 — Abrir novo chamado', Outbound.objects.latest('pk').text)
        self.say('1')
        self.assertEqual(Conversation.objects.get().state, 'target')
        self.assertEqual(Ticket.objects.count(), 1)
        self.client.force_login(self.agent)
        self.assertEqual(self.client.post(f'/support/inbox/{self.contact.pk}/',
            {'action': 'restart_bot'}).status_code, 302)
        self.assertEqual(Outbound.objects.count(), before + 2)

    def test_master_cannot_restart_bot_when_paused(self):
        BotConfig.objects.filter(pk=1).update(enabled=False)
        response = self.client.post(f'/support/inbox/{self.contact.pk}/', {'action': 'restart_bot'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Outbound.objects.filter(key__startswith='master-bot-menu:').exists())

    @patch('helpdesk.transport.gateway')
    def test_unresolved_lid_recovers_only_exact_verified_chat_id(self, gateway):
        lid = '262453002793117@lid'
        def response(method, path, *args):
            if '/lids/' in path:
                return {'pn': None}
            if path.startswith('/api/contacts?'):
                return {'id': lid, 'number': None}
            if 'check-exists' in path:
                return {'numberExists': True, 'chatId': lid if self.contact.phone[1:] in path else self.other.phone[1:] + '@c.us', 'pn': self.contact.phone[1:] + '@c.us' if self.contact.phone[1:] in path else self.other.phone[1:] + '@c.us'}
            raise AssertionError(path)
        gateway.side_effect = response
        event = self.payload('Oi', **{'from': lid})
        receive_event(event)
        self.assertTrue(Inbound.objects.filter(external_id=event['payload']['id']).exists())
        self.assertEqual(self.contact.verified_chat.chat_id, lid)

    @patch('helpdesk.transport.gateway')
    def test_unresolved_lid_does_not_assume_phone_when_check_exists_returns_pn_chat(self, gateway):
        lid = '262453002793117@lid'
        def response(method, path, *args):
            if '/lids/' in path:
                return {'pn': None}
            if 'check-exists' in path:
                return {'numberExists': True, 'chatId': self.contact.phone[1:] + '@c.us'}
            raise AssertionError(path)
        gateway.side_effect = response
        with self.assertRaises(ValidationError):
            receive_event(self.payload('Oi', **{'from': lid}))
        self.assertFalse(Inbound.objects.exists())

    @patch('helpdesk.transport.gateway')
    def test_noweb_contact_lookup_resolves_inbound_lid_and_opens_bot_flow(self, gateway):
        lid = '262453002793117@lid'
        def response(method, path, *args):
            if '/lids/' in path:
                return {'lid': lid, 'pn': None}
            if path.startswith('/api/contacts?'):
                return {'id': lid, 'number': self.contact.phone[1:]}
            raise AssertionError(path)
        gateway.side_effect = response
        event = self.payload('Oi', **{'from': lid})
        receive_event(event)
        received = Inbound.objects.get(external_id=event['payload']['id'])
        self.assertEqual(received.contact, self.contact)
        process_message(received.pk)
        self.assertEqual(Conversation.objects.get(contact=self.contact).state, 'offer')
        self.assertTrue(Outbound.objects.filter(contact=self.contact, key=f'inbound:{received.pk}').exists())
        self.assertEqual(self.contact.verified_chat.chat_id, lid)

    @patch('helpdesk.transport.gateway')
    def test_noweb_contact_lookup_rejects_unregistered_number(self, gateway):
        lid = '262453002793117@lid'
        def response(method, path, *args):
            if '/lids/' in path:
                return {'lid': lid, 'pn': None}
            if path.startswith('/api/contacts?'):
                return {'id': lid, 'number': '5581988889999'}
            if 'check-exists' in path:
                return {'numberExists': False}
            raise AssertionError(path)
        gateway.side_effect = response
        with self.assertRaises(ValidationError):
            receive_event(self.payload('Oi', **{'from': lid}))
        self.assertFalse(Inbound.objects.exists())
