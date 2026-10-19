import os
from datetime import datetime,timedelta,time
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from .models import Camera,PullCursor,CollectionGap,HistoryImport,RawDelivery,Reading,WhatsAppConfig,PreflightCheck,WhatsAppDelivery,NotificationConfig,MonitorSnapshot,SystemNotice,NoticeBatch
from .services import ZONE
from .history_pull import import_batch
from .reliability import process_recoveries
from .notifications import notice,resolve,dispatch_notices
from .preflight import preflight
from .verification import VerificationError

class AnchoredRecoveryTests(TestCase):
    def setUp(self):
        self.now=timezone.now();self.day=self.now.astimezone(ZONE).date()
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        self.anchor={'idIndex':100,'timestamp':int((self.now-timedelta(minutes=10)).timestamp()),'eventType':0,'stayTime':100}
        self.cp={'initialized':True,'index_mode':'id','offset':101,'last_id':100,'last_event':self.anchor}
        self.state=PullCursor.objects.create(camera=self.camera,initialized=True,index_mode='id',offset=103,last_id=102,last_event={'idIndex':102},last_success=self.now)
        self.events=[self.anchor,{'idIndex':101,'timestamp':int((self.now-timedelta(days=1)).timestamp()),'eventType':0},{'idIndex':102,'timestamp':int((self.now-timedelta(seconds=5)).timestamp()),'eventType':1}]
    def job(self):return HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,recovery_mode='tail',checkpoint=self.cp,automatic=True)
    def fetch(self,c,index,q):return [e for e in self.events if e['idIndex']>=index][:q]
    @patch('flow.history_pull.fetch')
    def test_tail_uses_checkpoint_and_keeps_clock_rollback_events_without_duplicates(self,fetch):
        fetch.side_effect=self.fetch;job=self.job();import_batch(job.pk)
        job.refresh_from_db();self.assertEqual(job.status,'completed');self.assertEqual(job.scanned,2)
        self.assertEqual(Reading.objects.count(),2);self.assertEqual(sum(Reading.objects.values_list('entries',flat=True)),1)
        self.assertTrue(all(c.args[1]>=100 for c in fetch.call_args_list))
        import_batch(job.pk);self.assertEqual(Reading.objects.count(),2)
    @patch('flow.history_pull.fetch')
    def test_changed_anchor_falls_back_without_removing_stored_events(self,fetch):
        fetch.return_value=[{**self.anchor,'timestamp':self.anchor['timestamp']-10}]
        raw=RawDelivery.objects.create(camera=self.camera,payload={})
        Reading.objects.create(camera=self.camera,event_id='pfc-event:99',timestamp=self.now,entries=1,exits=0,mode='delta',raw=raw)
        job=self.job();import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.recovery_mode,'full');self.assertEqual(job.status,'pending');self.assertFalse(job.initialized);self.assertEqual(Reading.objects.count(),1)
    @patch('flow.history_pull.fetch')
    def test_early_empty_page_cannot_certify_missing_tail(self,fetch):
        fetch.side_effect=lambda c,i,q:[] if i==101 and q==100 else self.fetch(c,i,q)
        job=self.job();import_batch(job.pk);job.refresh_from_db();self.assertEqual(job.recovery_mode,'full');self.assertNotEqual(job.status,'completed')
    @patch('flow.history_pull.fetch')
    def test_empty_tail_is_confirmed_without_scanning_old_history(self,fetch):
        self.state.offset=101;self.state.save()
        fetch.side_effect=lambda c,i,q:[self.anchor] if i==100 else []
        job=self.job();import_batch(job.pk);job.refresh_from_db();self.assertEqual(job.status,'completed');self.assertEqual(job.scanned,0)
        self.assertTrue(all(c.args[1]>=100 for c in fetch.call_args_list))
    @patch('flow.history_pull.fetch')
    def test_final_anchor_change_falls_back_and_keeps_inserted_events(self,fetch):
        seen={'end':0}
        def changing(c,i,q):
            page=self.fetch(c,i,q)
            if i==102 and q==1:
                seen['end']+=1
                if seen['end']>2:return [{**page[0],'timestamp':page[0]['timestamp']-1}]
            return page
        fetch.side_effect=changing;job=self.job();import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.recovery_mode,'full');self.assertEqual(job.status,'pending');self.assertEqual(Reading.objects.count(),2)
        from .models import DailyReport
        self.assertEqual(DailyReport.objects.get(day=self.day-timedelta(days=1)).data['totals']['entries'],1)
    @patch('flow.history_pull.fetch')
    def test_offset_mode_has_same_anchor_guards(self,fetch):
        self.cp['index_mode']='offset';self.cp['offset']=1;self.state.index_mode='offset';self.state.offset=3;self.state.save()
        fetch.side_effect=lambda c,i,q:self.events[i:i+q]
        job=self.job();import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.status,'completed');self.assertEqual(Reading.objects.count(),2)
    @patch('flow.history_pull.fetch')
    def test_changed_pagination_mode_cannot_use_old_checkpoint(self,fetch):
        self.state.index_mode='offset';self.state.save();job=self.job();import_batch(job.pk);job.refresh_from_db()
        fetch.assert_not_called();self.assertEqual(job.recovery_mode,'full')
    def test_grouping_restarts_one_automatic_scan_and_preserves_counts(self):
        job=HistoryImport.objects.create(camera=self.camera,start=self.day-timedelta(days=2),end=self.day-timedelta(days=2),automatic=True,status='running',initialized=True,offset=500,scanned=400,snapshot_at=self.now-timedelta(hours=1))
        a=CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(days=2),ended_at=self.now-timedelta(days=2)+timedelta(minutes=1),state='queued',job=job)
        b=CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(minutes=10),ended_at=self.now-timedelta(minutes=5),state='waiting')
        process_recoveries(self.now)
        a.refresh_from_db();b.refresh_from_db();job.refresh_from_db()
        self.assertEqual(a.job_id,b.job_id);self.assertEqual(HistoryImport.objects.count(),1);self.assertEqual(job.end,self.day);self.assertFalse(job.initialized)
        process_recoveries(self.now);self.assertEqual(HistoryImport.objects.count(),1)
    def test_paused_recovery_is_not_restarted_by_consolidation(self):
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='paused',automatic=True,offset=333)
        for n in [10,5]:CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(minutes=n),ended_at=self.now-timedelta(minutes=n-1),state='queued',job=job)
        process_recoveries(self.now);job.refresh_from_db();self.assertEqual(job.status,'paused');self.assertEqual(job.offset,333)
    def test_tail_completion_does_not_clear_an_unlinked_gap(self):
        HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='completed',recovery_mode='tail',snapshot_at=self.now)
        gap=CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(minutes=10),ended_at=self.now-timedelta(minutes=5),state='waiting')
        process_recoveries(self.now);gap.refresh_from_db();self.assertNotEqual(gap.state,'recovered')

class EarlyAndBundleTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,19,9,55,tzinfo=ZONE)
        NotificationConfig.objects.create(pk=1,enabled=True,recipient='5511999999999',activated_at=self.now)
        WhatsAppConfig.objects.create(pk=1,enabled=True,chat_ids=['test@g.us'],send_times=['10:10'])
    @patch('flow.preflight.verified_total',return_value=123)
    def test_early_check_never_sends_or_reuses_total_for_scheduled_send(self,verify):
        with patch('django.utils.timezone.now',return_value=self.now):preflight(self.now);preflight(self.now)
        verify.assert_called_once();self.assertEqual(PreflightCheck.objects.get().original_entries,123);self.assertFalse(WhatsAppDelivery.objects.exists())
        self.assertEqual(PreflightCheck.objects.get().status,'ready')
    @patch('flow.preflight.verified_total',side_effect=VerificationError('Reexame pendente'))
    def test_early_failure_is_visible_before_slot(self,verify):
        with patch('django.utils.timezone.now',return_value=self.now):preflight(self.now)
        self.assertEqual(PreflightCheck.objects.get().status,'blocked');self.assertIn('Reexame pendente',SystemNotice.objects.get().reason)
    @patch('flow.whatsapp.gateway',return_value={'id':'ok'})
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    def test_alerts_are_bundled_then_normalized_once(self,session,gateway):
        notice('a','Contador A','Pendente',self.now);notice('b','Contador B','Pendente',self.now)
        dispatch_notices(self.now);self.assertEqual(gateway.call_count,1);self.assertEqual(NoticeBatch.objects.get().references.__len__(),2)
        resolve('a',self.now);resolve('b',self.now)
        dispatch_notices(self.now+timedelta(minutes=1));dispatch_notices(self.now+timedelta(minutes=2))
        self.assertEqual(gateway.call_count,2);self.assertIn('SITUAÇÃO NORMALIZADA',gateway.call_args.args[2]['text'])
        self.assertEqual(SystemNotice.objects.filter(recovery_status='accepted').count(),2)
    @patch('flow.whatsapp.gateway',return_value={'id':'ok'})
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    def test_digest_is_rate_limited_and_excludes_resolved_incidents(self,session,gateway):
        notice('a','Contador A','Pendente',self.now);dispatch_notices(self.now)
        dispatch_notices(self.now+timedelta(minutes=29));self.assertEqual(gateway.call_count,1)
        dispatch_notices(self.now+timedelta(minutes=31));self.assertEqual(gateway.call_count,2);self.assertIn('ANDAMENTO',gateway.call_args.args[2]['text'])
    def test_external_health_is_minimal_and_stale_snapshot_fails(self):
        MonitorSnapshot.objects.create(pk=1,healthy=True,checked_at=timezone.now())
        response=self.client.get('/health/monitor/');self.assertEqual(response.status_code,200);self.assertEqual(set(response.json()),{'status','observed_at'})
        MonitorSnapshot.objects.update(checked_at=timezone.now()-timedelta(minutes=4))
        self.assertEqual(self.client.get('/health/monitor/').status_code,503)
        self.assertFalse(SystemNotice.objects.exists())
