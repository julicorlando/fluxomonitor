import os
from datetime import datetime,time,timedelta
from unittest.mock import patch
from django.test import TestCase
from django.utils import timezone
from django.contrib.auth.models import User
from flow.models import Camera,HistoryImport,Reading,PullCursor
from flow.services import ZONE
from flow.history_pull import import_batch
from flow.history_views import ImportForm

class HistoryTests(TestCase):
    def setUp(self):
        self.end_patch=patch('flow.history_pull.find_end',return_value=1000000)
        self.end_patch.start();self.addCleanup(self.end_patch.stop)
        self.camera=Camera.objects.create(ip='192.168.1.235',name='Entrada')
        self.day=timezone.now().astimezone(ZONE).date()-timedelta(days=3)
    def event(self,i,day,kind):
        return {'idIndex':i,'timestamp':int(datetime.combine(day,time(12),tzinfo=ZONE).timestamp()),'eventType':kind}
    @patch('flow.history_pull.close_day')
    @patch('flow.history_pull.fetch')
    def test_history_range_dedup_and_independent_live_cursor(self,fetch,close):
        events=[self.event(1,self.day,0),self.event(2,self.day,1),self.event(3,self.day,4),self.event(4,self.day+timedelta(days=1),0)]
        fetch.return_value=events
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,initialized=True,offset=1)
        cursor=PullCursor.objects.create(camera=self.camera,offset=999,last_id=998,initialized=True,index_mode='id')
        import_batch(job.pk)
        job.refresh_from_db();self.assertEqual(job.status,'running');self.assertEqual(job.imported,2)
        self.assertEqual(Reading.objects.count(),2)
        cursor.refresh_from_db();self.assertEqual(cursor.offset,999)
        close.assert_called_with(self.day)
        repeat=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,initialized=True,offset=1)
        import_batch(repeat.pk);repeat.refresh_from_db();self.assertEqual(repeat.imported,0)
        self.assertEqual(Reading.objects.count(),2)
    @patch('flow.history_pull.fetch',return_value=[])
    def test_empty_history_completes(self,fetch):
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day)
        import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.status,'completed');self.assertEqual(job.imported,0)
        self.assertTrue(job.initialized)
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235,192.168.1.237'})
    def test_import_form_limits_and_replica_source(self):
        today=timezone.now().astimezone(ZONE).date()
        bad=ImportForm({'camera':self.camera.pk,'start':str(today-timedelta(days=100)),'end':str(today)})
        self.assertFalse(bad.is_valid())
        source=Camera.objects.create(ip='192.168.1.237',name='A')
        replica=Camera.objects.create(ip='192.168.1.237',name='B')
        good=ImportForm({'camera':replica.pk,'start':str(self.day),'end':str(today)})
        self.assertTrue(good.is_valid(),good.errors);self.assertEqual(good.cleaned_data['camera'].pk,source.pk)
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235'})
    def test_staff_create_and_cancel_import(self):
        staff=User.objects.create_user('operator',password='test',is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/imports/').status_code,200)
        response=self.client.post('/imports/',{'camera':self.camera.pk,'start':str(self.day),'end':str(self.day)})
        self.assertEqual(response.status_code,302)
        job=HistoryImport.objects.get()
        self.client.post('/imports/',{'action':'cancel','job_id':job.pk})
        job.refresh_from_db();self.assertEqual(job.status,'cancelled')
        with patch('flow.history_pull.fetch') as fetch:
            import_batch(job.pk);fetch.assert_not_called()

    @patch('flow.history_pull.close_day')
    @patch('flow.history_pull.fetch')
    def test_clock_rollback_does_not_stop_at_end_date(self,fetch,close):
        events=[self.event(10,self.day+timedelta(days=2),0),self.event(11,self.day,0),self.event(12,self.day,1)]
        fetch.side_effect=[events,[]]
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,initialized=True,offset=10)
        import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.status,'running');self.assertEqual(job.imported,2)
        self.assertEqual(job.offset,13);self.assertEqual(job.scanned,3)
        import_batch(job.pk);job.refresh_from_db();self.assertEqual(job.status,'completed')

    @patch('flow.history_pull.close_day')
    @patch('flow.history_pull.fetch')
    def test_history_starts_at_oldest_id_without_timestamp_search(self,fetch,close):
        old=self.event(100,self.day-timedelta(days=5),0)
        outside=self.event(101,self.day+timedelta(days=1),0)
        inside=self.event(102,self.day,0)
        events=[old,outside,inside]
        fetch.side_effect=lambda camera,index,quantity: ([old,outside] if index==0 else [] if index==1 else [e for e in events if e['idIndex']>=index][:quantity])
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day)
        import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.index_mode,'id');self.assertEqual(job.scanned,3)
        self.assertEqual(job.imported,1);self.assertEqual(job.offset,103)
        self.assertEqual(Reading.objects.get().event_id,'pfc-event:102')

    def test_retry_resets_scan_but_keeps_readings_and_live_cursor(self):
        staff=User.objects.create_user('retry',is_staff=True)
        self.client.force_login(staff)
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='failed',initialized=True,offset=100,scanned=99,error='Eventos fora de ordem')
        cursor=PullCursor.objects.create(camera=self.camera,offset=500)
        self.client.post('/imports/',{'action':'retry','job_id':job.pk})
        job.refresh_from_db();cursor.refresh_from_db()
        self.assertEqual(job.status,'pending');self.assertFalse(job.initialized)
        self.assertEqual(job.scanned,0);self.assertEqual(job.error,'');self.assertEqual(cursor.offset,500)
        self.assertContains(self.client.get('/imports/'),'Parar importação')

    def test_retry_does_not_create_concurrent_jobs_or_allow_non_staff(self):
        failed=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='failed')
        active=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day)
        staff=User.objects.create_user('staff',is_staff=True)
        self.client.force_login(staff)
        self.client.post('/imports/',{'action':'retry','job_id':failed.pk})
        failed.refresh_from_db();self.assertEqual(failed.status,'failed')
        active.status='completed';active.save()
        viewer=User.objects.create_user('viewer')
        self.client.force_login(viewer)
        self.client.post('/imports/',{'action':'retry','job_id':failed.pk})
        failed.refresh_from_db();self.assertEqual(failed.status,'failed')

    def test_progress_bounds_and_endpoint_access(self):
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,first_offset=100,target_offset=300,offset=200)
        self.assertEqual(job.progress,50)
        job.offset=400;self.assertEqual(job.progress,99.9)
        job.status='completed';self.assertEqual(job.progress,100)
        job.save()
        self.assertEqual(self.client.get('/imports/progress/').status_code,302)
        user=User.objects.create_user('progress-staff',is_staff=True)
        self.client.force_login(user)
        response=self.client.get('/imports/progress/')
        self.assertEqual(response.json()['jobs'][0]['progress'],100)
        self.assertEqual(response['Cache-Control'],'no-store')
        self.assertContains(self.client.get('/imports/'),'aria-label="Progresso da importação"')
        user.is_staff=False;user.save()
        self.assertEqual(self.client.get('/imports/progress/').status_code,302)

    @patch('flow.history_pull.fetch')
    def test_fixed_limit_completes_even_when_new_events_arrive(self,fetch):
        fetch.return_value=[self.event(100,self.day,0),self.event(101,self.day,1)]
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,initialized=True,offset=100,first_offset=100,target_offset=101)
        import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.status,'completed');self.assertEqual(job.progress,100)
        self.assertEqual(job.scanned,1);self.assertEqual(job.imported,1)

class HistoryLimitTests(TestCase):
    @patch('flow.history_pull.fetch')
    def test_find_end_handles_absolute_id_gaps_and_offset(self,fetch):
        from flow.history_pull import find_end
        ids=[100,103,110]
        fetch.side_effect=lambda camera,index,quantity:[{'idIndex':i} for i in ids if i>=index][:quantity]
        self.assertEqual(find_end(None,100),111)
        fetch.side_effect=lambda camera,index,quantity:[{}] if index<3 else []
        self.assertEqual(find_end(None,0),3)

class HistoryPriorityTests(TestCase):
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235'})
    @patch('flow.management.commands.import_pfc_history.import_batch')
    def test_today_runs_before_older_jobs(self,run):
        from django.core.management import call_command
        camera=Camera.objects.create(ip='192.168.1.235',name='A')
        day=timezone.now().astimezone(ZONE).date()
        HistoryImport.objects.create(camera=camera,start=day-timedelta(days=10),end=day-timedelta(days=1))
        today=HistoryImport.objects.create(camera=camera,start=day,end=day)
        call_command('import_pfc_history',once=True)
        run.assert_called_once_with(today.pk)
    @patch('flow.import_cleanup.reimport_today',side_effect=ValueError('Bloqueio de fonte'))
    def test_expected_guard_is_visible_instead_of_500(self,reset):
        user=User.objects.create_user('guard-admin',is_superuser=True,is_staff=True)
        self.client.force_login(user)
        response=self.client.post('/imports/',{'action':'reimport_today'},follow=True)
        self.assertContains(response,'Bloqueio de fonte')
        self.assertFalse(HistoryImport.objects.exists())

class ParallelHistoryTests(TestCase):
    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235,192.168.1.236,192.168.1.237,192.168.1.237'})
    @patch('flow.management.commands.import_pfc_history.import_batch')
    def test_maintenance_pass_processes_each_physical_ip_once(self,run):
        from django.core.management import call_command
        day=timezone.now().astimezone(ZONE).date();jobs=[]
        for ip in ['192.168.1.235','192.168.1.236','192.168.1.237']:
            camera=Camera.objects.create(name=ip,ip=ip)
            jobs.append(HistoryImport.objects.create(camera=camera,start=day,end=day))
            HistoryImport.objects.create(camera=camera,start=day-timedelta(days=2),end=day-timedelta(days=1))
        call_command('import_pfc_history',once=True)
        self.assertEqual([c.args[0] for c in run.call_args_list],[j.pk for j in jobs])

    @patch('flow.management.commands.import_pfc_history.connections.close_all')
    @patch('flow.management.commands.import_pfc_history.close_old_connections')
    @patch('flow.management.commands.import_pfc_history.next_job')
    @patch('flow.management.commands.import_pfc_history.import_batch')
    def test_slow_device_does_not_hold_other_device(self,run,select,close_old,close_all):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from types import SimpleNamespace
        from flow.management.commands.import_pfc_history import run_batch
        entered=Event();release=Event();fast_finished=Event()
        select.side_effect=lambda ip:SimpleNamespace(pk=ip)
        def batch(ip):
            if ip=='slow':entered.set();self.assertTrue(release.wait(3))
            else:fast_finished.set()
        run.side_effect=batch
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow=pool.submit(run_batch,'slow')
            try:
                self.assertTrue(entered.wait(2))
                fast=pool.submit(run_batch,'fast')
                self.assertTrue(fast_finished.wait(2));self.assertFalse(slow.done())
            finally:release.set()
            slow.result();fast.result()
        self.assertEqual(close_all.call_count,2)
