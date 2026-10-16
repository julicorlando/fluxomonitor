import importlib.util,os,tempfile
from pathlib import Path
from datetime import date,datetime,time,timedelta
from unittest.mock import patch,MagicMock
from django.test import TestCase,Client
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Camera,Reading,RawDelivery,PullCursor,HistoryImport,CollectionGap,ReportRevision,BackupRun,DeviceAlert,OpeningHours,CalendarException
from .services import ZONE,close_day
from .reliability import observe_collection,process_recoveries
from .intelligence import day_check,comparable_days,comparable_analysis,flow_month,weekly_executive

class ReliabilityTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,16,12,tzinfo=ZONE);self.day=self.now.date()
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        PullCursor.objects.create(camera=self.camera,initialized=True,last_success=self.now)
    def event(self,day,clock=time(10),entries=1,exits=0,camera=None):
        camera=camera or self.camera;raw=RawDelivery.objects.create(camera=camera,payload={})
        return Reading.objects.create(camera=camera,event_id=f'pfc-event:{raw.pk}',timestamp=datetime.combine(day,clock,ZONE),entries=entries,exits=exits,mode='delta',raw=raw)
    def test_quiet_success_does_not_queue_recovery_and_real_failure_does_once(self):
        observe_collection(self.camera,True,self.now-timedelta(seconds=5),self.now)
        self.assertEqual(CollectionGap.objects.count(),0)
        observe_collection(self.camera,False,self.now-timedelta(minutes=5),self.now)
        observe_collection(self.camera,False,self.now-timedelta(minutes=5),self.now)
        self.assertEqual(CollectionGap.objects.count(),1)
        observe_collection(self.camera,True,self.now-timedelta(minutes=5),self.now)
        process_recoveries(self.now);process_recoveries(self.now)
        self.assertEqual(HistoryImport.objects.count(),1)
        gap=CollectionGap.objects.get();self.assertEqual(gap.state,'queued')
        gap.job.status='completed';gap.job.snapshot_at=self.now;gap.job.save();process_recoveries(self.now)
        gap.refresh_from_db();self.assertEqual(gap.state,'recovered')
    def test_active_pause_is_respected_and_resume_preserves_cursor(self):
        gap=CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(minutes=5),ended_at=self.now,state='waiting')
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='paused',offset=123)
        process_recoveries(self.now);gap.refresh_from_db();self.assertEqual(gap.job_id,job.pk)
        process_recoveries(self.now);job.refresh_from_db();self.assertEqual(job.status,'paused')
        job.status='failed';job.error='URLError';job.save()
        process_recoveries(self.now+timedelta(minutes=16));job.refresh_from_db()
        self.assertEqual(job.status,'pending');self.assertEqual(job.offset,123)
        job.status='failed';job.error='idIndex reutilizado';job.save()
        process_recoveries(self.now+timedelta(hours=1));gap.refresh_from_db()
        self.assertEqual(gap.state,'review')
    def test_old_snapshot_cannot_clear_gap_but_new_manual_scan_can(self):
        gap=CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(minutes=5),ended_at=self.now,state='queued')
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='completed',snapshot_at=self.now-timedelta(minutes=10))
        gap.job=job;gap.save()
        process_recoveries(self.now);gap.refresh_from_db();self.assertNotEqual(gap.state,'recovered')
        job.snapshot_at=self.now+timedelta(minutes=1);job.save()
        gap.state='review';gap.save();process_recoveries(self.now+timedelta(minutes=2))
        gap.refresh_from_db();self.assertEqual(gap.state,'recovered')

    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235','PFC_SERVICE_SECRET':'test'})
    @patch('flow.verification.poll',return_value=0)
    def test_late_final_verification_excludes_entries_after_close(self,poll):
        from .verification import verified_total
        later=self.now.replace(hour=23)
        PullCursor.objects.filter(camera=self.camera).update(last_success=later)
        self.event(self.day,time(21),entries=1)
        self.event(self.day,time(22,11),entries=1)
        with patch('django.utils.timezone.now',return_value=later):self.assertEqual(verified_total(self.day),1)

    def test_old_gap_requires_review_no_fake_recovery(self):
        gap=CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(days=91),ended_at=self.now,state='waiting')
        process_recoveries(self.now);gap.refresh_from_db()
        self.assertEqual(gap.state,'review');self.assertFalse(HistoryImport.objects.exists())
    def test_revision_records_before_after_once(self):
        day=self.day-timedelta(days=1);self.event(day,entries=2)
        close_day(day);self.event(day,entries=3)
        close_day(day,reason='Importação #9');close_day(day)
        revision=ReportRevision.objects.get()
        self.assertEqual(revision.before['totals']['entries'],2)
        self.assertEqual(revision.after['totals']['entries'],5)
        self.assertEqual(revision.reason,'Importação #9')
    def test_day_check_pending_gap_and_closing_cut(self):
        self.event(self.day,entries=2);self.event(self.day,time(23),entries=900)
        check=day_check(self.day,self.now.replace(hour=23,minute=30))
        self.assertEqual(check['entries'],2);self.assertFalse(check['partial'])
        CollectionGap.objects.create(camera=self.camera,started_at=self.now)
        check=day_check(self.day,self.now)
        self.assertTrue(check['blocked']);self.assertTrue(any('comunicação' in s for s in check['devices'][0]['blockers']))
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235','PFC_SERVICE_SECRET':'test'})
    @patch('flow.verification.poll',return_value=0)
    def test_unrecovered_transport_gap_blocks_verification(self,poll):
        from .verification import verified_total,VerificationError
        CollectionGap.objects.create(camera=self.camera,started_at=self.now-timedelta(minutes=5),ended_at=self.now,state='waiting')
        with patch('django.utils.timezone.now',return_value=self.now):
            with self.assertRaisesMessage(VerificationError,'recuperação'):verified_total(self.day)
        poll.assert_not_called()

    def test_comparable_weekdays_exclude_holidays_and_align_elapsed(self):
        for n,a in [(7,4),(14,6),(21,8),(28,10)]:self.event(self.day-timedelta(days=n),entries=a)
        holiday=self.day-timedelta(days=7)
        CalendarException.objects.create(day=holiday,closed=False,is_holiday=True,description='Feriado local')
        self.assertNotIn(holiday,comparable_days(self.day))
        self.event(self.day,entries=12)
        result=comparable_analysis(self.day,self.now)
        self.assertEqual(result['average'],8);self.assertEqual(result['variation'],50)
        self.assertTrue(result['partial'])
    def test_month_and_weekly_totals_respect_close_and_replica(self):
        self.event(self.day,entries=3);self.event(self.day,time(23),entries=500)
        source=Camera.objects.create(name='Original',ip='192.168.1.237')
        replica=Camera.objects.create(name='Replica',ip='192.168.1.237')
        for c in (source,replica):self.event(self.day,entries=2,camera=c)
        month=flow_month(self.day,self.now.replace(hour=23))
        self.assertEqual(month['entries'],7)
        week=weekly_executive(self.day-timedelta(days=self.day.weekday()),self.now.replace(hour=23))
        self.assertEqual(week['entries'],7);self.assertEqual(sum(d['entries'] for d in week['devices']),7)
    def test_open_week_compares_same_elapsed_time_not_full_previous_week(self):
        self.event(self.day,entries=2)
        self.event(self.day-timedelta(days=7),time(10),entries=4)
        self.event(self.day-timedelta(days=7),time(13),entries=100)
        self.event(self.day-timedelta(days=6),time(10),entries=100)
        week=weekly_executive(self.day-timedelta(days=self.day.weekday()),self.now)
        self.assertEqual(week['entries'],2);self.assertEqual(week['previous_entries'],4)
        self.assertEqual(week['variation'],-50);self.assertTrue(week['partial'])
        self.assertTrue(week['days'][-1]['future'])

    def test_idle_alert_only_in_configured_hours_and_resolves(self):
        from .operations import monitor_devices
        self.camera.last_seen=self.now;self.camera.save()
        monitor_devices(self.now);self.assertFalse(DeviceAlert.objects.filter(kind='idle').exists())
        OpeningHours.objects.create(weekday=self.day.weekday(),opens=time(9),closes=time(18))
        self.event(self.day,time(10))
        monitor_devices(self.now);monitor_devices(self.now)
        self.assertEqual(DeviceAlert.objects.filter(kind='idle',resolved_at__isnull=True).count(),1)
        self.event(self.day,time(11,59));monitor_devices(self.now)
        self.assertFalse(DeviceAlert.objects.filter(kind='idle',resolved_at__isnull=True).exists())
    def test_pages_permissions_csrf_and_backup_queue(self):
        viewer=User.objects.create_user('viewer');self.client.force_login(viewer)
        with patch('django.utils.timezone.now',return_value=self.now):
            for page in ['/flow-calendar/','/analysis/','/executive/']:
                self.assertEqual(self.client.get(page).status_code,200,page)
            for page in ['/checks/','/backups/']:self.assertEqual(self.client.get(page).status_code,302)
            viewer.is_staff=True;viewer.is_superuser=True;viewer.save()
            for page in ['/checks/','/backups/']:self.assertEqual(self.client.get(page).status_code,200)
            client=Client(enforce_csrf_checks=True);client.force_login(viewer)
            self.assertEqual(client.post('/backups/').status_code,403)
            self.client.post('/backups/');self.client.post('/backups/')
            self.assertEqual(BackupRun.objects.count(),1)

class BackupWorkerTests(TestCase):
    def test_only_marks_verified_after_successful_restore_and_uses_snapshot(self):
        spec=importlib.util.spec_from_file_location('backup_worker',Path(__file__).resolve().parent.parent/'backup'/'worker.py')
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        source=MagicMock();source.cursor.return_value.__enter__.return_value.fetchone.return_value=['snapshot-1']
        admin=MagicMock();admin.cursor.return_value.__enter__.return_value.fetchone.return_value=[100]
        expected={'flow_reading':{'rows':2,'a':'3','b':'4'}}
        with tempfile.TemporaryDirectory() as folder:
            def dump(args,**kwargs):Path(args[-1]).write_bytes(b'test dump')
            with patch.object(worker,'connect',return_value=source),patch.object(worker,'manifest',return_value=expected),patch.object(worker,'run_tool',side_effect=dump) as tool,patch.object(worker,'restore_check',return_value=expected) as restore:
                self.assertTrue(worker.perform(3,admin,Path(folder)))
                self.assertIn('snapshot-1',tool.call_args.args[0]);restore.assert_called_once()
                self.assertTrue(any("status='verified'" in str(call) for call in admin.cursor.return_value.__enter__.return_value.execute.call_args_list))
            admin.reset_mock();admin.cursor.return_value.__enter__.return_value.fetchone.return_value=[100]
            with patch.object(worker,'connect',return_value=source),patch.object(worker,'manifest',return_value=expected),patch.object(worker,'run_tool',side_effect=dump),patch.object(worker,'restore_check',side_effect=ValueError('secret password should never leak')):
                self.assertFalse(worker.perform(4,admin,Path(folder)))
                calls=str(admin.cursor.return_value.__enter__.return_value.execute.call_args_list)
                self.assertNotIn('secret password',calls);self.assertNotIn("status='verified'",calls)
