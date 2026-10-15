import os
from datetime import timedelta,datetime,time
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .models import Camera,PullCursor,Reading,RawDelivery,HistoryImport,AuditLog,WhatsAppConfig,WhatsAppDelivery
from .services import ZONE
from .verification import verified_total,VerificationError

@patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235','PFC_SERVICE_SECRET':'test'})
class VerificationTests(TestCase):
    def setUp(self):
        self.day=timezone.now().astimezone(ZONE).date()
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        PullCursor.objects.create(camera=self.camera,initialized=True,last_success=timezone.now())
    def reading(self,camera=None,kind=0):
        camera=camera or self.camera
        raw=RawDelivery.objects.create(camera=camera,payload={})
        return Reading.objects.create(camera=camera,event_id='pfc-event:10',timestamp=timezone.now()-timedelta(seconds=10),entries=int(kind==0),exits=int(kind==1),mode='delta',raw=raw)
    @patch('flow.verification.poll',return_value=0)
    def test_zero_and_total_require_two_empty_confirmations(self,poll):
        self.assertEqual(verified_total(self.day),0);self.assertEqual(poll.call_count,2)
        self.reading();poll.reset_mock()
        self.assertEqual(verified_total(self.day),1);self.assertEqual(poll.call_count,2)
        self.assertEqual(AuditLog.objects.filter(action='whatsapp_verified').last().changes['entries'],1)
    @patch('flow.verification.poll',side_effect=[0,3,0,0])
    def test_backlog_resets_confirmation_counter(self,poll):
        self.assertEqual(verified_total(self.day),0);self.assertEqual(poll.call_count,4)
    @patch('flow.verification.poll',return_value=100)
    def test_unfinished_backlog_blocks(self,poll):
        with self.assertRaisesMessage(VerificationError,'aguardando coleta'):verified_total(self.day)
        self.assertFalse(AuditLog.objects.filter(action='whatsapp_verified').exists())
    @patch('flow.verification.poll',side_effect=TimeoutError)
    def test_device_failure_blocks(self,poll):
        with self.assertRaisesMessage(VerificationError,'consulta não confirmada'):verified_total(self.day)
    @patch('flow.verification.poll')
    def test_pending_history_and_uninitialized_block(self,poll):
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='failed')
        with self.assertRaisesMessage(VerificationError,'Histórico'):verified_total(self.day)
        poll.assert_not_called();job.status='completed';job.save()
        PullCursor.objects.update(initialized=False)
        with self.assertRaisesMessage(VerificationError,'inicializada'):verified_total(self.day)
    @patch('flow.verification.poll',return_value=0)
    def test_mixed_source_future_and_wrong_sum_block(self,poll):
        r=self.reading();r.event_id='push:10';r.save()
        with self.assertRaisesMessage(VerificationError,'inconsistente'):verified_total(self.day)
        r.event_id='pfc-event:10';r.timestamp=timezone.now()+timedelta(seconds=30);r.save()
        with self.assertRaisesMessage(VerificationError,'futuro'):verified_total(self.day)
        r.timestamp=timezone.now()-timedelta(seconds=10);r.save()
        with patch('flow.verification.report',return_value={'cameras':[],'totals':{'entries':999}}):
            with self.assertRaisesMessage(VerificationError,'divergiu'):verified_total(self.day)
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235,192.168.1.237'})
    @patch('flow.verification.poll',return_value=0)
    @patch('flow.verification.mirror')
    def test_replica_must_match_and_intentional_double_is_preserved(self,mirror,poll):
        source=Camera.objects.create(name='Original',ip='192.168.1.237')
        target=Camera.objects.create(name='Replica',ip='192.168.1.237')
        PullCursor.objects.create(camera=source,initialized=True,last_success=timezone.now())
        r=self.reading(source)
        with self.assertRaisesMessage(VerificationError,'réplica difere'):verified_total(self.day)
        raw=RawDelivery.objects.create(camera=target,payload={})
        Reading.objects.create(camera=target,event_id=r.event_id,timestamp=r.timestamp,entries=r.entries,exits=r.exits,mode='delta',raw=raw)
        self.assertEqual(verified_total(self.day),2)
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('flow.whatsapp.gateway',return_value={'id':'ok'})
    def test_schedule_blocks_then_rechecks_final_later_without_duplicates(self,gateway,session):
        from .whatsapp import scheduled_send,send_test
        cfg=WhatsAppConfig.objects.create(pk=1,enabled=True,chat_ids=['one@c.us','two@g.us'])
        now=datetime.combine(self.day,time(22,10),tzinfo=ZONE)
        with patch('flow.whatsapp.verified_total',side_effect=VerificationError('fila pendente')):
            scheduled_send(now);gateway.assert_not_called()
            with self.assertRaisesMessage(ValueError,'Teste bloqueado'):send_test(cfg,'test')
        self.assertEqual(WhatsAppDelivery.objects.filter(status='skipped').count(),2)
        with patch('flow.whatsapp.verified_total',return_value=40) as verify:
            scheduled_send(now+timedelta(minutes=30));scheduled_send(now+timedelta(minutes=31))
            self.assertEqual(verify.call_count,1)
        self.assertEqual(gateway.call_count,2)
        self.assertEqual(WhatsAppDelivery.objects.filter(status='accepted').count(),2)

    @patch('flow.verification.poll',return_value=0)
    def test_cleaned_today_blocks_until_new_history_is_complete(self,poll):
        from .models import ImportCleanup
        batch=ImportCleanup.objects.create(actor='test',affected_days={str(self.day):[self.camera.pk]})
        with self.assertRaisesMessage(VerificationError,'dados importados limpos'):verified_total(self.day)
        HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='completed')
        self.assertEqual(verified_total(self.day),0)
