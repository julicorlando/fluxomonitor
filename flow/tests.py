import hashlib,json
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from django.test import TestCase
from django.utils import timezone
from django.core.management import call_command
from .models import Camera,ApiKey,Reading,DailyReport
from .services import report
class FlowTests(TestCase):
 def setUp(self):
  self.c=Camera.objects.create(name='Entrada',ip='192.168.1.235')
  ApiKey.objects.create(name='test',digest=hashlib.sha256(b'test').hexdigest(),camera=self.c,can_ingest=True)
  self.auth={'HTTP_AUTHORIZATION':'Bearer test'}
  self.day=timezone.localdate()-timedelta(days=1)
 def send(self,eid,ins,outs,mode='delta',hour=12,day=None,epoch=''):
  stamp=datetime.combine(day or self.day,datetime.min.time(),ZoneInfo('America/Sao_Paulo'))+timedelta(hours=hour)
  return self.client.post('/api/v1/ingest/',json.dumps({'event_id':eid,'timestamp':stamp.isoformat(),'entries':ins,'exits':outs,'mode':mode,'counter_epoch':epoch}),content_type='application/json',**self.auth)
 def test_auth(self):self.assertEqual(self.client.get('/api/v1/live/').status_code,401)
 def test_duplicate_and_conflict(self):
  self.assertEqual(self.send('a',10,3).status_code,201)
  self.assertEqual(self.send('a',10,3).status_code,200)
  self.assertEqual(self.send('a',11,3).status_code,400)
  self.assertEqual(Reading.objects.count(),1)
  self.assertEqual(report(self.day)['totals']['balance'],7)
 def test_cumulative(self):
  self.send('a',100,50,'cumulative',10)
  self.send('b',110,53,'cumulative',11)
  self.assertEqual(report(self.day)['totals']['entries'],10)
 def test_reset_without_epoch(self):
  self.send('a',100,50,'cumulative',10)
  self.send('b',2,1,'cumulative',11)
  d=report(self.day);self.assertEqual(d['totals']['entries'],0);self.assertTrue(d['warnings'])
 def test_epoch(self):
  self.send('a',100,50,'cumulative',10,epoch='one')
  self.send('b',2,1,'cumulative',11,epoch='two')
  self.assertEqual(report(self.day)['totals']['entries'],2)
 def test_day_zero_and_close(self):
  self.send('a',10,3)
  call_command('close_days');self.assertTrue(DailyReport.objects.filter(day=self.day).exists())
  self.assertEqual(report(self.day+timedelta(days=1))['totals']['balance'],0)
 def test_capture(self):
  r=self.client.post('/api/camera/dataUpload',json.dumps({'native':123}),content_type='application/json',**self.auth)
  self.assertEqual(r.status_code,202);self.assertEqual(Reading.objects.count(),0)
 def test_late_out_of_order(self):
  self.send('b',110,55,'cumulative',11)
  self.send('a',100,50,'cumulative',10)
  self.assertEqual(report(self.day)['totals']['entries'],10)
 def test_login_dashboard(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('tester',password='Strong-example-123');self.client.force_login(u)
  self.assertContains(self.client.get('/'),'Movimento em perspectiva')
 def test_invalid(self):self.assertEqual(self.send('a',-1,0).status_code,400)
 def test_device_create_and_edit(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('manager',password='test',is_staff=True);self.client.force_login(u)
  r=self.client.post('/devices/',{'name':'Contador 4','ip':'192.168.1.238','enabled':'on'})
  self.assertEqual(r.status_code,302);self.assertTrue(Camera.objects.filter(ip='192.168.1.238').exists())
  self.assertContains(self.client.get('/devices/'),'Contador 4')
 def test_device_scope(self):
  other=Camera.objects.create(name='Outro',ip='192.168.1.236')
  r=self.client.get('/api/v1/devices/',**self.auth)
  self.assertEqual(len(r.json()['devices']),1)
  self.assertEqual(self.client.get(f'/api/v1/devices/{other.id}/',**self.auth).status_code,403)
  self.assertEqual(self.client.get(f'/api/v1/devices/{other.id}/readings/',**self.auth).status_code,403)
 def test_device_counter_api(self):
  self.send('a',10,3)
  r=self.client.get(f'/api/v1/devices/{self.c.id}/?date={self.day}',**self.auth)
  self.assertEqual(r.json()['latest_counter']['entries'],10)
  self.assertEqual(r.json()['day']['totals']['balance'],7)
  self.assertEqual(len(self.client.get(f'/api/v1/devices/{self.c.id}/readings/',**self.auth).json()['readings']),1)
 def test_device_management_restricted(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('viewer',password='test');self.client.force_login(u)
  self.assertEqual(self.client.get('/devices/').status_code,403)
