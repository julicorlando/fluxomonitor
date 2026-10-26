import importlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from flow.models import AuditLog
from .bindings import remember_sender, recover_bound_pending
from .models import (Company, Contact, Conversation, Inbound, Outbound, SenderIdentity,
                     Ticket, UnresolvedInbound, VerifiedChatIdentity)
from .services import process_message
from .transport import dispatch, receive_event, resolve_phone, verified_destination


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class PersistentBindingTests(TestCase):
    lid = '123456789123456@lid'

    def setUp(self):
        self.company = Company.objects.get(code='SC')
        self.person = Contact.objects.create(company=self.company, name='Solicitante', phone='+5581999991234', email='person@example.com')
        self.other = Contact.objects.create(company=Company.objects.get(code='CM'), name='Outra pessoa', phone='+5581999995678', email='other@example.com')
        self.master = get_user_model().objects.create_user('master', is_staff=True, is_superuser=True)
        self.client.force_login(self.master)
        self.serial = 0

    def pending(self, external='first', text='Olá', status='unresolved'):
        return UnresolvedInbound.objects.create(external_id=external, sender=self.lid, text=text,
            sent_at=timezone.now()-timedelta(minutes=1), status=status)

    def confirm_once(self):
        event = self.pending()
        result = self.client.post(f'/support/inbox/{self.person.pk}/', {
            'action':'verify_lid', 'lid':self.lid, 'identity_confirmed':'yes'})
        self.assertEqual(result.status_code, 302)
        item = Inbound.objects.get(external_id=event.external_id)
        process_message(item.pk)
        return SenderIdentity.objects.get(chat_id=self.lid)

    def receive(self, text, external=None):
        self.serial += 1
        identifier = external or 'reply-' + str(self.serial)
        receive_event({'session':'default','event':'message','payload':{
            'id':identifier,'from':self.lid,'fromMe':False,'body':text,
            'timestamp':int(timezone.now().timestamp())+self.serial}})
        return identifier

    def test_one_confirmation_survives_all_bot_replies_and_opens_ticket(self):
        binding = self.confirm_once()
        with patch('helpdesk.transport.session_info', return_value={'status':'WORKING'}), patch('helpdesk.transport.gateway') as gateway:
            def response(method, path, body=None):
                # No GET is necessary once the master confirmed the sender.
                self.assertEqual((method,path), ('POST','/api/sendText'))
                self.assertEqual(body['chatId'], self.lid)
                return {'id':'sent-' + str(gateway.call_count)}
            gateway.side_effect = response
            for answer, expected in [('1','target'), ('Financeiro','description'), ('Sem acesso ao servidor','confirm'), ('1','idle')]:
                dispatch()
                # Reproduce the original failure: the mutable cache becomes a PN after every send.
                VerifiedChatIdentity.objects.update_or_create(contact=self.person, defaults={
                    'phone':self.person.phone,'chat_id':self.person.phone[1:]+'@c.us'})
                external = self.receive(answer)
                item = Inbound.objects.get(external_id=external)
                self.assertEqual(item.contact, self.person)
                process_message(item.pk)
                self.assertEqual(Conversation.objects.get(contact=self.person).state, expected)
                self.assertFalse(UnresolvedInbound.objects.filter(status='unresolved').exists())
                binding.refresh_from_db()
                self.assertEqual((binding.chat_id,binding.source), (self.lid,'master'))
            dispatch()
            self.assertEqual(Ticket.objects.get().code, 'SC-00001')
            self.assertEqual(gateway.call_count, 5)
            self.assertFalse(Outbound.objects.exclude(status='accepted').exists())

    @patch('helpdesk.transport.gateway', side_effect=HTTPError('http://waha',503,'Unavailable',{},None))
    def test_bound_sender_is_identified_without_any_gateway_lookup(self, gateway):
        self.confirm_once()
        external = self.receive('1')
        self.assertEqual(Inbound.objects.get(external_id=external).contact, self.person)
        gateway.assert_not_called()

    def test_multiple_sender_ids_remain_bound_to_same_person(self):
        first = remember_sender(self.person, self.lid, source='master', actor='master')
        remember_sender(self.person, '123456789@lid', source='gateway')
        self.assertEqual(SenderIdentity.objects.filter(contact=self.person).count(), 2)
        self.assertEqual(resolve_phone({'from':self.lid}), self.person.phone)
        self.assertEqual(resolve_phone({'from':'123456789@lid'}), self.person.phone)
        self.assertEqual(verified_destination(self.person), first.chat_id)

    def test_cold_object_reload_keeps_association(self):
        self.confirm_once()
        fresh = Contact.objects.get(pk=self.person.pk)
        self.assertEqual(verified_destination(fresh), self.lid)
        self.assertEqual(resolve_phone({'from':self.lid}), fresh.phone)

    def test_deactivated_or_changed_phone_does_not_get_automatic_access(self):
        self.confirm_once()
        self.person.active=False
        self.person.save()
        with patch('helpdesk.transport.gateway') as gateway:
            self.assertIsNone(resolve_phone({'from':self.lid}))
            gateway.assert_not_called()
        self.person.active=True
        self.person.phone='+5581988881122'
        self.person.save()
        self.assertIsNone(resolve_phone({'from':self.lid}))
        self.assertNotEqual(SenderIdentity.objects.get().phone, self.person.phone)
        with self.assertRaises(ValidationError):
            remember_sender(self.person, self.lid, source='gateway')

    def test_identity_cannot_be_reassigned_to_another_person(self):
        remember_sender(self.person,self.lid,source='master',actor='master')
        with self.assertRaises(ValidationError):
            remember_sender(self.other,self.lid,source='master',actor='master')
        self.assertEqual(SenderIdentity.objects.get().contact, self.person)

    def test_automatic_refresh_cannot_downgrade_master_confirmation(self):
        binding=remember_sender(self.person,self.lid,source='master',actor='master')
        remember_sender(self.person,self.lid,source='gateway')
        binding.refresh_from_db()
        self.assertEqual((binding.source,binding.confirmed_by), ('master','master'))

    def test_pending_messages_recover_without_another_manual_click_and_are_idempotent(self):
        remember_sender(self.person,self.lid,source='master',actor='master')
        event=self.pending('held-response','1')
        recover_bound_pending(); recover_bound_pending()
        self.assertEqual(Inbound.objects.filter(external_id=event.external_id).count(),1)
        event.refresh_from_db()
        self.assertEqual(event.status,'linked')

    def test_recovery_never_relabels_message_already_owned_by_another_company(self):
        remember_sender(self.person,self.lid,source='master',actor='master')
        event=self.pending('collision','1')
        Inbound.objects.create(external_id=event.external_id, contact=self.other, company=self.other.company,
            phone=self.other.phone,text='Existing',sent_at=event.sent_at)
        recover_bound_pending()
        self.assertEqual(Inbound.objects.get(external_id=event.external_id).contact,self.other)
        event.refresh_from_db()
        self.assertEqual(event.status,'unresolved')

    def test_ui_shows_persistent_link_and_manual_confirmation_remains_required_first_time(self):
        self.confirm_once()
        response=self.client.get(f'/support/inbox/{self.person.pk}/')
        self.assertContains(response,'Vínculo permanente ativo')
        self.assertContains(response,self.lid)
        self.assertEqual(SenderIdentity.objects.count(),1)

    @patch('helpdesk.transport.gateway', return_value={'lid':'123456789123456@lid','pn':'5581999991234@c.us'})
    def test_gateway_verified_incoming_mapping_is_remembered(self, gateway):
        external=self.receive('Olá')
        self.assertTrue(Inbound.objects.filter(external_id=external).exists())
        self.assertEqual(SenderIdentity.objects.get(chat_id=self.lid).source,'gateway')
        gateway.reset_mock()
        self.receive('1')
        gateway.assert_not_called()

    def restore(self):
        migration=importlib.import_module('helpdesk.migrations.0008_restore_sender_bindings')
        migration.restore_sender_bindings(apps,SimpleNamespace(connection=connection))

    def test_migration_recovers_manual_link_overwritten_by_outbound_cache(self):
        event=self.pending('historical',status='linked')
        Inbound.objects.create(external_id=event.external_id,contact=self.person,company=self.person.company,
            phone=self.person.phone,text=event.text,sent_at=event.sent_at)
        AuditLog.objects.create(actor='master',action='support_lid_verified',object_type='Contact',object_id=str(self.person.pk),
            changes={'lid':self.lid,'contact_id':self.person.pk,'verification':'master_confirmed_out_of_band'})
        VerifiedChatIdentity.objects.create(contact=self.person,phone=self.person.phone,chat_id=self.person.phone[1:]+'@c.us')
        self.restore(); self.restore()
        binding=SenderIdentity.objects.get()
        self.assertEqual((binding.chat_id,binding.contact,binding.source), (self.lid,self.person,'master'))

    def test_migration_does_not_restore_a_link_after_contact_phone_changed(self):
        event=self.pending('historical',status='linked')
        Inbound.objects.create(external_id=event.external_id,contact=self.person,company=self.person.company,
            phone=self.person.phone,text=event.text,sent_at=event.sent_at)
        AuditLog.objects.create(actor='master',action='support_lid_verified',object_type='Contact',object_id=str(self.person.pk),
            changes={'lid':self.lid,'contact_id':self.person.pk,'verification':'master_confirmed_out_of_band'})
        self.person.phone='+5581988881122';self.person.save()
        self.restore()
        self.assertFalse(SenderIdentity.objects.exists())

    def test_migration_keeps_conflicting_legacy_identity_unassigned(self):
        for person in (self.person,self.other):
            VerifiedChatIdentity.objects.create(contact=person,phone=person.phone,chat_id=self.lid)
        self.restore()
        self.assertFalse(SenderIdentity.objects.exists())

    def test_migration_preserves_valid_legacy_mapping(self):
        VerifiedChatIdentity.objects.create(contact=self.person,phone=self.person.phone,chat_id=self.lid)
        self.restore()
        self.assertEqual(SenderIdentity.objects.get().contact,self.person)
