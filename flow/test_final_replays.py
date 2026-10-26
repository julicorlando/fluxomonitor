from datetime import date,time,timedelta
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase,Client
from django.utils import timezone
from .adjustments import adjusted_entries
from .final_replays import recent_finals,preview,enqueue,dispatch_final_replays
from .models import WhatsAppConfig,WhatsAppDelivery,WhatsAppFinalReplay,WhatsAppFinalReplayRecipient,AuditLog

class FinalReplayTests(TestCase):
    def setUp(self):
        self.cfg=WhatsAppConfig.objects.create(pk=1,chat_ids=['5511999999999@c.us','test@g.us'],error_margin_percent=25)
        self.admin=User.objects.create_superuser('admin','admin@example.com','password')
        self.client.force_login(self.admin)
        for day,total in [(20,50),(21,100),(22,101),(23,0),(24,999)]:
            self.sent(day,total)
        self.sent(24,999,chat='other@g.us')
        self.sent(25,10000,status='unknown')
        WhatsAppDelivery.objects.create(day=date(2026,10,25),scheduled_time=time(18),chat_id='test@g.us',status='accepted',text='Boa noite fluxo parcial 10000')
    def sent(self,day,total,chat='test@g.us',status='accepted',percent=10):
        adjustment=adjusted_entries(total,percent)
        return WhatsAppDelivery.objects.create(day=date(2026,10,day),scheduled_time=time(21) if day==24 else time(22,10),chat_id=chat,status=status,text=f'*MENSAGEM AUTOMATICA*\n\nfluxo final {adjustment["adjusted"]}',adjustment=adjustment)
    def queue(self):return enqueue(preview('admin')['token'],'admin',destination=self.cfg.chat_ids[0])[0]
    def test_last_three_distinct_days_only_sent_finals_and_zero(self):
        rows=recent_finals()
        self.assertEqual([r['day'] for r in rows],['2026-10-22','2026-10-23','2026-10-24'])
        self.assertEqual([r['adjusted'] for r in rows],[90,0,899])
        self.assertEqual(len(rows[-1]['source_ids']),2)
    def test_discount_uses_original_snapshot_not_current_margin_or_adjusted_total(self):
        WhatsAppDelivery.objects.filter(day=date(2026,10,24)).delete()
        self.sent(24,101,percent=25)
        self.assertEqual(recent_finals()[-1]['adjusted'],90)
    def test_invalid_or_missing_snapshot_blocks_instead_of_guessing(self):
        WhatsAppDelivery.objects.filter(day=date(2026,10,24)).update(adjustment=None)
        with self.assertRaisesMessage(ValueError,'sem memória'):preview('admin')
    def test_inconsistent_historical_totals_block(self):
        self.sent(24,500,chat='third@g.us')
        with self.assertRaisesMessage(ValueError,'totais diferentes'):preview('admin')
    def test_message_snapshot_mismatch_blocks(self):
        WhatsAppDelivery.objects.filter(day=date(2026,10,24)).update(text='fluxo final 800')
        with self.assertRaisesMessage(ValueError,'divergentes'):preview('admin')
    def test_double_click_does_not_duplicate_and_schedules_unchanged(self):
        token=preview('admin')['token'];before=WhatsAppDelivery.objects.count()
        a,created=enqueue(token,'admin',destination='test@g.us');b,again=enqueue(token,'admin',destination='test@g.us')
        self.assertTrue(created);self.assertFalse(again);self.assertEqual(a.pk,b.pk)
        self.assertEqual(a.deliveries.count(),1);self.assertEqual(WhatsAppDelivery.objects.count(),before)
        self.assertIn('22/10/2026 · Fluxo: 90',a.text);self.assertNotIn('Fluxo: 81',a.text);self.assertNotIn('desconto',a.text.lower())
        self.assertEqual(a.deliveries.get().chat_id,'test@g.us')
        with self.assertRaisesMessage(ValueError,'fila'):self.queue()
    def test_changed_recipients_or_history_require_new_preview(self):
        token=preview('admin')['token'];self.cfg.chat_ids=['new@g.us'];self.cfg.save()
        with self.assertRaisesMessage(ValueError,'mudaram'):enqueue(token,'admin',destination='test@g.us')
        self.assertFalse(WhatsAppFinalReplay.objects.exists())
        token=preview('admin')['token'];self.sent(26,100)
        with self.assertRaisesMessage(ValueError,'mudaram'):enqueue(token,'admin',destination='new@g.us')
    def test_invalid_token_and_other_actor_rejected(self):
        with self.assertRaises(ValueError):enqueue('forged','admin')
        with self.assertRaises(ValueError):enqueue(preview('admin')['token'],'other')

    def test_destination_is_required_and_unknown_saved_destination_rejected(self):
        token=preview('admin')['token']
        for value in ['', 'unknown@g.us', '5511888888888@c.us']:
            with self.assertRaises(ValueError):enqueue(token,'admin',destination=value)
        for number in ['', 'not-a-number', '5511888888888@g.us','123', '0'*15]:
            with self.assertRaises(ValueError):enqueue(token,'admin',destination='custom',number=number)
        self.assertFalse(WhatsAppFinalReplay.objects.exists())

    @patch('flow.final_replays.gateway',return_value={'id':'ok'})
    @patch('flow.final_replays.session_info',return_value={'status':'WORKING'})
    def test_manual_number_is_only_destination_and_does_not_change_schedule(self,session,gateway):
        before=list(self.cfg.chat_ids)
        replay,created=enqueue(preview('admin')['token'],'admin',destination='custom',number='+55 (11) 98888-8888')
        self.assertTrue(created);self.assertEqual(replay.manual_chat_id,'5511988888888@c.us')
        self.assertEqual(replay.deliveries.get().chat_id,replay.manual_chat_id)
        dispatch_final_replays();dispatch_final_replays()
        gateway.assert_called_once_with('POST','/api/sendText',{'session':'default','chatId':'5511988888888@c.us','text':replay.text})
        self.cfg.refresh_from_db();self.assertEqual(self.cfg.chat_ids,before)
        self.assertNotIn('desconto',replay.text.lower());self.assertIn('Fluxo: 90',replay.text)

    def test_manual_number_works_without_saved_contacts(self):
        self.cfg.chat_ids=[];self.cfg.chat_id='';self.cfg.save()
        token=preview('admin')['token'];self.assertTrue(token)
        replay,_=enqueue(token,'admin',destination='custom',number='5511988888888')
        self.assertEqual(replay.deliveries.count(),1)

    def test_same_preview_cannot_be_reused_for_different_destination(self):
        token=preview('admin')['token']
        a,_=enqueue(token,'admin',destination='test@g.us')
        with self.assertRaisesMessage(ValueError,'outro destino'):
            enqueue(token,'admin',destination='custom',number='5511988888888')
        self.assertEqual(a.deliveries.count(),1);self.assertEqual(WhatsAppFinalReplay.objects.count(),1)
    @patch('flow.final_replays.gateway',side_effect=[{'id':'ok'},TimeoutError()])
    @patch('flow.final_replays.session_info',return_value={'status':'WORKING'})
    def test_worker_is_per_recipient_and_never_retries_uncertain(self,session,gateway):
        replay=self.queue()
        # A legacy queued replay can still contain several saved recipients.
        WhatsAppFinalReplayRecipient.objects.create(replay=replay,chat_id='test@g.us')
        dispatch_final_replays();dispatch_final_replays()
        self.assertEqual(gateway.call_count,2)
        self.assertEqual(list(replay.deliveries.order_by('id').values_list('status',flat=True)),['accepted','unknown'])
        self.assertTrue(all(call.args[2]['text']==replay.text for call in gateway.call_args_list))
        self.assertEqual(WhatsAppDelivery.objects.filter(status='accepted').count(),7)
    @patch('flow.final_replays.gateway')
    @patch('flow.final_replays.session_info',return_value={'status':'STOPPED'})
    def test_disconnected_queues_then_expires_without_sending(self,session,gateway):
        replay=self.queue();dispatch_final_replays();self.assertEqual(replay.deliveries.filter(status='pending').count(),1)
        WhatsAppFinalReplay.objects.filter(pk=replay.pk).update(created_at=timezone.now()-timedelta(minutes=16))
        dispatch_final_replays();gateway.assert_not_called();self.assertEqual(replay.deliveries.filter(status='blocked').count(),1)
    @patch('flow.final_replays.gateway')
    @patch('flow.final_replays.session_info',return_value={'status':'WORKING'})
    def test_removed_recipient_and_interrupted_send_not_retried(self,session,gateway):
        replay=self.queue();WhatsAppFinalReplayRecipient.objects.create(replay=replay,chat_id='test@g.us');one=replay.deliveries.order_by('id').first();one.status='sending';one.attempted_at=timezone.now()-timedelta(minutes=3);one.save()
        self.cfg.chat_ids=[];self.cfg.save();dispatch_final_replays();gateway.assert_not_called()
        self.assertEqual(set(replay.deliveries.values_list('status',flat=True)),{'unknown','blocked'})
    def test_empty_history_or_single_day(self):
        WhatsAppDelivery.objects.all().delete();self.assertEqual(preview('admin')['token'],'')
        self.sent(24,100);self.assertEqual(len(preview('admin')['rows']),1)
    @patch('flow.whatsapp_views.session_info',return_value={'status':'STOPPED'})
    def test_button_preview_and_history_render_without_sending(self,session):
        response=self.client.get('/whatsapp/')
        self.assertContains(response,'Reenviar fluxos finais');self.assertContains(response,'22/10/2026');self.assertContains(response,'Enviar para');self.assertNotContains(response,'Fluxos finais · desconto')
        self.assertFalse(WhatsAppFinalReplay.objects.exists())
        self.assertEqual(response.headers['Cache-Control'],'no-store')
    def test_post_only_superadmin_csrf_and_queue_without_gateway(self):
        self.assertEqual(self.client.get('/whatsapp/finals/replay/').status_code,405)
        secure=Client(enforce_csrf_checks=True);secure.force_login(self.admin)
        self.assertEqual(secure.post('/whatsapp/finals/replay/',{'preview_token':preview('admin')['token'],'destination':'test@g.us'}).status_code,403)
        response=self.client.post('/whatsapp/finals/replay/',{'preview_token':preview('admin')['token'],'destination':'test@g.us'})
        self.assertEqual(response.status_code,302);self.assertEqual(WhatsAppFinalReplay.objects.count(),1)
        audit=AuditLog.objects.get(action='wa_final_replay')
        audit.full_clean()  # SQLite does not enforce PostgreSQL varchar lengths.
        staff=User.objects.create_user('staff',is_staff=True);self.client.force_login(staff)
        self.assertEqual(self.client.post('/whatsapp/finals/replay/',{}).status_code,302)
        self.assertEqual(WhatsAppFinalReplay.objects.count(),1)

    @patch('flow.whatsapp_views.enqueue_final_replay')
    def test_database_error_is_logged_and_shown_without_blank_500(self,enqueue_mock):
        from django.db import DataError
        enqueue_mock.side_effect=DataError('database constraint')
        with self.assertLogs('flow.whatsapp_views',level='ERROR'):
            response=self.client.post('/whatsapp/finals/replay/',{'preview_token':'test'})
        self.assertEqual(response.status_code,302)
        self.assertFalse(WhatsAppFinalReplay.objects.exists())
        from django.contrib.messages import get_messages
        self.assertIn('Confira o histórico',str(list(get_messages(response.wsgi_request))[0]))
