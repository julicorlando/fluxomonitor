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
        job.refresh_from_db();self.assertEqual(job.status,'completed');self.assertEqual(job.imported,2)
        self.assertEqual(Reading.objects.count(),2)
        cursor.refresh_from_db();self.assertEqual(cursor.offset,999)
        close.assert_called_with(self.day)
        repeat=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,initialized=True,offset=1)
        import_batch(repeat.pk);repeat.refresh_from_db();self.assertEqual(repeat.imported,0)
        self.assertEqual(Reading.objects.count(),2)
    @patch('flow.history_pull.fetch',return_value=[])
    @patch('flow.history_pull.locate_today',return_value=(42,'id'))
    def test_empty_history_completes(self,locate,fetch):
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day)
        import_batch(job.pk);job.refresh_from_db()
        self.assertEqual(job.status,'completed');self.assertEqual(job.imported,0)
        locate.assert_called_once_with(self.camera,self.day)
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
