import hashlib,io,json
from datetime import datetime,time,date,timedelta
from unittest.mock import patch
from PIL import Image
from django.test import TestCase,Client
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from .models import Camera,RawDelivery,Reading,ApiKey,HistoryImport,AccessMap
from .services import ZONE,report

class InsightsTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('analyst');self.client.force_login(self.user)
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        self.other=Camera.objects.create(name='Fora',ip='192.168.1.236',include_in_total=False)
        self.now=datetime(2026,10,15,12,30,tzinfo=ZONE)
    def reading(self,camera,stamp,a,b,mode='delta',epoch=''):
        raw=RawDelivery.objects.create(camera=camera,payload={})
        return Reading.objects.create(camera=camera,event_id=str(raw.pk),timestamp=stamp,entries=a,exits=b,mode=mode,counter_epoch=epoch,raw=raw)
    def get(self,**params):
        with patch('django.utils.timezone.now',return_value=self.now):return self.client.get('/api/v1/insights/',params)
    def test_elapsed_day_comparison_and_future_records(self):
        self.reading(self.camera,self.now-timedelta(minutes=60),4,1)
        self.reading(self.camera,self.now+timedelta(hours=1),999,999)
        self.reading(self.camera,self.now-timedelta(days=1,hours=1),2,1)
        self.reading(self.camera,self.now-timedelta(days=1)+timedelta(hours=1),500,0)
        self.reading(self.other,self.now-timedelta(minutes=2),100,0)
        response=self.get(date='2026-10-15',mode='day');self.assertEqual(response.status_code,200)
        d=response.json();self.assertEqual(d['current']['totals']['entries'],4);self.assertEqual(d['previous']['totals']['entries'],2)
        self.assertEqual(d['variation'],100);self.assertFalse(d['conclusive']);self.assertTrue(d['current']['partial'])
        self.assertTrue(next(p for p in d['current']['points'] if p['label']=='13:00')['future'])
    def test_month_year_and_export_sum_same_records(self):
        for day in [date(2026,1,3),date(2026,10,2),date(2026,10,15)]:self.reading(self.camera,datetime.combine(day,time(10),ZONE),3,1)
        self.assertEqual(self.get(mode='month',date='2026-10-15').json()['current']['totals']['entries'],6)
        self.assertEqual(self.get(mode='year',date='2026-10-15').json()['current']['totals']['entries'],9)
        response=self.get(mode='year',date='2026-10-15',format='csv');self.assertEqual(response.status_code,200);self.assertIn('text/csv',response['Content-Type'])
        self.assertContains(response,'Nao certificada')
    def test_replica_factor_and_missing_data_warning(self):
        a=Camera.objects.create(name='Original',ip='192.168.1.237');b=Camera.objects.create(name='Replica',ip='192.168.1.237')
        for c in (a,b):self.reading(c,self.now-timedelta(hours=1),5,1)
        d=self.get(date='2026-10-15').json()['current'];self.assertEqual(d['totals']['entries'],10);self.assertIn('Entrada',d['missing_devices'])
        HistoryImport.objects.create(camera=a,start=self.now.date(),end=self.now.date(),status='paused')
        self.assertTrue(self.get(date='2026-10-15').json()['current']['pending_imports'])
    def test_cumulative_uses_existing_reset_rules(self):
        self.reading(self.camera,self.now-timedelta(days=1),90,50,'cumulative')
        self.reading(self.camera,self.now-timedelta(hours=1),100,54,'cumulative')
        self.reading(self.camera,self.now-timedelta(minutes=20),2,1,'cumulative')
        d=self.get(date='2026-10-15').json()['current'];self.assertTrue(d['legacy']);self.assertEqual(d['totals']['entries'],10);self.assertEqual(d['totals']['exits'],4)
    def test_auth_scope_and_invalid_inputs(self):
        self.client.logout();self.assertEqual(self.get().status_code,401)
        ApiKey.objects.create(name='limited',camera=self.camera,digest=hashlib.sha256(b'key').hexdigest(),can_read=True)
        self.assertEqual(self.client.get('/api/v1/insights/',{'device':self.other.pk},HTTP_AUTHORIZATION='Bearer key').status_code,403)
        self.client.force_login(self.user)
        for params in [{'mode':'invalid'},{'date':'bad'},{'date':'2026-10-16'},{'grain':7},{'device':'abc'},{'date':'2000-01-01','comparison':'year'}]:
            response=self.get(**params)
            self.assertEqual(response.status_code,200 if params=={'date':'2000-01-01','comparison':'year'} else 400)
        self.assertEqual(self.get(mode='invalid').status_code,400)
    def test_day_interval_entries_exits_match_totals(self):
        self.reading(self.camera,self.now-timedelta(minutes=15),3,2)
        d=report(self.now.date())
        self.assertEqual(sum(p['entries'] for p in d['intervals']),d['totals']['entries'])
        self.assertEqual(sum(p['exits'] for p in d['intervals']),d['totals']['exits'])

class MapAndPauseTests(TestCase):
    def setUp(self):
        self.staff=User.objects.create_user('operator',is_staff=True)
        self.viewer=User.objects.create_user('viewer')
        self.camera=Camera.objects.create(name='Acesso',ip='192.168.1.235')
        self.client.force_login(self.staff)
    def test_position_validation_and_viewer_cannot_change(self):
        data={'positions':{str(self.camera.pk):{'x':25,'y':75}}}
        self.assertEqual(self.client.post('/map/positions/',data=json.dumps(data),content_type='application/json').status_code,200)
        self.assertEqual(AccessMap.objects.get().positions[str(self.camera.pk)]['x'],25)
        for pos in [{'x':-1,'y':3},{'x':float('nan'),'y':50},{'x':'12','y':50}]:
            self.assertEqual(self.client.post('/map/positions/',data=json.dumps({'positions':{str(self.camera.pk):pos}}),content_type='application/json').status_code,400)
        self.client.force_login(self.viewer);self.assertEqual(self.client.post('/map/positions/',data=json.dumps(data),content_type='application/json').status_code,403)
        self.assertEqual(self.client.get('/map/').status_code,200)
    def test_private_image_validated_and_reencoded(self):
        output=io.BytesIO();Image.new('RGB',(20,20),'white').save(output,'PNG')
        response=self.client.post('/map/',{'plan':SimpleUploadedFile('floor.png',output.getvalue(),content_type='image/png')})
        self.assertEqual(response.status_code,302)
        image=self.client.get('/map/image/');self.assertEqual(image['Content-Type'],'image/jpeg');self.assertIn('no-store',image['Cache-Control'])
        self.assertContains(self.client.post('/map/',{'plan':SimpleUploadedFile('bad.svg',b'<svg></svg>')}),'imagem PNG')
        self.client.logout();self.assertEqual(self.client.get('/map/image/').status_code,302)
    def test_pause_resume_preserves_cursor_and_blocks_parallel_same_camera(self):
        day=timezone.localdate();job=HistoryImport.objects.create(camera=self.camera,start=day,end=day,status='running',offset=999,initialized=True,scanned=100)
        self.client.post('/imports/',{'action':'pause','job_id':job.pk});job.refresh_from_db();self.assertEqual(job.status,'paused')
        with patch('flow.history_pull.fetch') as fetch:
            from .history_pull import import_batch
            import_batch(job.pk);fetch.assert_not_called()
        self.client.post('/imports/',{'action':'resume','job_id':job.pk});job.refresh_from_db();self.assertEqual(job.status,'pending');self.assertEqual(job.offset,999);self.assertEqual(job.scanned,100)
        job.status='paused';job.save();HistoryImport.objects.create(camera=self.camera,start=day,end=day)
        self.client.post('/imports/',{'action':'resume','job_id':job.pk});job.refresh_from_db();self.assertEqual(job.status,'paused')
    def test_app_shell_and_public_manifest(self):
        self.assertContains(self.client.get('/'),'Mapa dos acessos');self.assertContains(self.client.get('/'),'gesti/dashboard.js')
        self.client.logout();self.assertEqual(self.client.get('/manifest.webmanifest').json()['display'],'standalone')
        response=self.client.get('/sw.js');self.assertIn('javascript',response['Content-Type']);self.assertIn('no-cache',response['Cache-Control'])

class MapCsrfTests(TestCase):
    def test_layout_write_requires_csrf_even_for_staff(self):
        user=User.objects.create_user('csrf-map',is_staff=True)
        camera=Camera.objects.create(name='Map',ip='192.168.1.235')
        client=Client(enforce_csrf_checks=True);client.force_login(user)
        body=json.dumps({'positions':{str(camera.pk):{'x':50,'y':50}}})
        self.assertEqual(client.post('/map/positions/',body,content_type='application/json').status_code,403)
        client.get('/map/');token=client.cookies['csrftoken'].value
        self.assertEqual(client.post('/map/positions/',body,content_type='application/json',HTTP_X_CSRFTOKEN=token).status_code,200)
