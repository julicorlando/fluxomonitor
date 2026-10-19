import os
from datetime import datetime,time,timedelta
from urllib.error import HTTPError
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from django.core.management import call_command
from .models import NotificationConfig,SystemNotice,ServicePulse,Camera,PullCursor,CollectionGap,WhatsAppConfig,WhatsAppDelivery
from .services import ZONE
from .notifications import notice,resolve,scan_system,dispatch_notices,runtime_failure

class NoticeTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,19,10,13,tzinfo=ZONE)
        self.cfg=NotificationConfig.objects.create(pk=1,enabled=True,recipient='5511999999999',activated_at=self.now-timedelta(hours=1))
    def test_incident_deduplicates_and_new_episode_rearms(self):
        notice('test','Erro','Motivo',self.now);notice('test','Erro','Motivo atualizado',self.now)
        self.assertEqual(SystemNotice.objects.count(),1)
        SystemNotice.objects.update(status='accepted',accepted_at=self.now)
        notice('test','Erro','Ainda aberto',self.now)
        self.assertEqual(SystemNotice.objects.get().status,'accepted')
        resolve('test',self.now);notice('test','Erro','Nova ocorrência',self.now+timedelta(minutes=5))
        item=SystemNotice.objects.get();self.assertEqual(item.status,'pending');self.assertIsNone(item.resolved_at);self.assertIsNone(item.accepted_at)
    @patch('flow.whatsapp.gateway',return_value={'id':'ok'})
    @patch('flow.whatsapp.session_info',return_value={'status':'STOPPED'})
    def test_disconnected_notice_waits_then_sends_once_to_configured_private_recipient(self,session,gateway):
        notice('test','Falha','Sem conexão',self.now)
        dispatch_notices(self.now);gateway.assert_not_called()
        self.assertEqual(SystemNotice.objects.get().status,'pending')
        session.return_value={'status':'WORKING'}
        later=self.now+timedelta(minutes=2)
        dispatch_notices(later);dispatch_notices(later)
        gateway.assert_called_once()
        self.assertEqual(gateway.call_args.args[2]['chatId'],'5511999999999@c.us')
        self.assertIn('Sem conexão',gateway.call_args.args[2]['text'])
        self.assertEqual(SystemNotice.objects.get().status,'accepted')
    @patch('flow.whatsapp.gateway',side_effect=TimeoutError)
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    def test_uncertain_result_is_not_duplicated(self,session,gateway):
        notice('test','Falha','Erro',self.now)
        dispatch_notices(self.now);dispatch_notices(self.now+timedelta(hours=1))
        gateway.assert_called_once();self.assertEqual(SystemNotice.objects.get().status,'unknown')
    @patch('flow.whatsapp.gateway',side_effect=HTTPError('http://gateway',429,'limit',{},None))
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    def test_explicit_rate_limit_backs_off(self,session,gateway):
        notice('test','Falha','Erro',self.now)
        dispatch_notices(self.now);dispatch_notices(self.now+timedelta(seconds=30))
        gateway.assert_called_once();self.assertEqual(SystemNotice.objects.get().status,'pending')
        self.assertGreater(SystemNotice.objects.get().next_attempt,self.now)
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    def test_independent_watchdog_detects_missed_slot_and_retains_reason(self,session):
        flow=WhatsAppConfig.objects.create(pk=1,enabled=True,chat_ids=['group@g.us'],send_times=['10:10','16:10','22:10'])
        scan_system(self.now-timedelta(minutes=2));self.assertFalse(SystemNotice.objects.filter(key__startswith='flow:').exists())
        delivery=WhatsAppDelivery.objects.create(day=self.now.date(),scheduled_time=time(10,10),chat_id='group@g.us',status='skipped',text='',detail='Histórico ainda pendente')
        scan_system(self.now);scan_system(self.now)
        notice_record=SystemNotice.objects.get(key='flow:2026-10-19:10:10');self.assertIn('Histórico ainda pendente',notice_record.reason)
        self.assertIn('0/1',notice_record.reason)
        delivery.status='accepted';delivery.save();scan_system(self.now)
        notice_record.refresh_from_db();self.assertIsNotNone(notice_record.resolved_at)
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    def test_offline_workers_and_device_deduplication(self,session):
        camera=Camera.objects.create(name='Acesso',ip='192.168.1.237')
        Camera.objects.create(name='Replica',ip='192.168.1.237')
        CollectionGap.objects.create(camera=camera,started_at=self.now-timedelta(minutes=10),state='waiting',ended_at=self.now)
        scan_system(self.now)
        self.assertEqual(SystemNotice.objects.filter(key__startswith='device:').count(),1)
        self.assertEqual(SystemNotice.objects.filter(key__startswith='worker:').count(),3)
        for name in ['collector','history','closer']:ServicePulse.objects.create(name=name,checked_at=self.now)
        CollectionGap.objects.update(state='recovered');scan_system(self.now)
        self.assertFalse(SystemNotice.objects.filter(key__startswith='worker:',resolved_at__isnull=True).exists())
        self.assertFalse(SystemNotice.objects.filter(key__startswith='device:',resolved_at__isnull=True).exists())
    def test_no_alert_before_activation_and_no_secret_exception_text(self):
        runtime_failure('collector',ValueError('secret=EXAMPLE_TOKEN'))
        item=SystemNotice.objects.get();self.assertNotIn('EXAMPLE_TOKEN',item.reason);self.assertIn('ValueError',item.reason)
        self.cfg.enabled=False;self.cfg.save();notice('disabled','Não enviar','disabled')
        self.assertFalse(SystemNotice.objects.filter(key='disabled').exists())
    @patch('flow.whatsapp.session_info',return_value={'status':'STOPPED'})
    def test_schedule_failure_is_recorded_immediately(self,session):
        from .whatsapp import scheduled_send
        WhatsAppConfig.objects.create(pk=1,enabled=True,chat_ids=['group@g.us'],send_times=['10:10'])
        scheduled_send(self.now-timedelta(minutes=3));scheduled_send(self.now-timedelta(minutes=3))
        self.assertEqual(WhatsAppDelivery.objects.get().status,'skipped')
        self.assertEqual(SystemNotice.objects.filter(key__startswith='flow:').count(),1)
        self.assertIn('WhatsApp desconectado',SystemNotice.objects.get(key__startswith='flow:').reason)
    def test_only_superadmin_can_configure_number_and_activation_is_stable(self):
        viewer=User.objects.create_user('viewer');self.client.force_login(viewer)
        self.assertEqual(self.client.post('/alerts/',{'enabled':'on','recipient':'5511888888888'}).status_code,403)
        self.assertNotContains(self.client.get('/alerts/'),'Ocorrências e avisos de erro')
        admin=User.objects.create_superuser('owner','a@example.com','test');self.client.force_login(admin)
        self.assertContains(self.client.get('/alerts/'),'Ocorrências e avisos de erro')
        activated=self.cfg.activated_at
        self.assertEqual(self.client.post('/alerts/',{'enabled':'on','recipient':'5511888888888'}).status_code,302)
        self.cfg.refresh_from_db();self.assertEqual(self.cfg.activated_at,activated)
        self.assertEqual(self.client.post('/alerts/',{'enabled':'on','recipient':'invalid'}).status_code,200)
        self.cfg.refresh_from_db();self.assertEqual(self.cfg.recipient,'5511888888888')
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235','PFC_SERVICE_SECRET':'test'})
    @patch('flow.management.commands.poll_pfc.process_recoveries')
    @patch('flow.management.commands.poll_pfc.mirror')
    @patch('flow.management.commands.poll_pfc.poll',return_value=0)
    def test_reconnection_starts_automatic_recovery_without_polluting_collection(self,poll,mirror,recovery):
        from django.utils import timezone
        camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        PullCursor.objects.create(camera=camera,initialized=True,last_success=timezone.now())
        CollectionGap.objects.create(camera=camera,started_at=timezone.now(),state='open')
        recovery.side_effect=RuntimeError('recovery error')
        call_command('poll_pfc',once=True)
        recovery.assert_called_once();mirror.assert_not_called()
        state=PullCursor.objects.get(camera=camera);self.assertEqual(state.last_error,'')
        self.assertEqual(CollectionGap.objects.get().state,'waiting')
        self.assertTrue(SystemNotice.objects.filter(key='runtime:collector-recovery').exists())
