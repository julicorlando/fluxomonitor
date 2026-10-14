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
  r=self.client.post('/devices/',{'name':'Contador 4','ip':'192.168.1.238','enabled':'on','include_in_total':'on','expected_interval':60})
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

 def test_management_pages(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('root',password='test',is_staff=True,is_superuser=True);self.client.force_login(u)
  for url in ['/reports/','/groups/','/tokens/','/users/','/diagnostics/',f'/devices/{self.c.id}/']:
   self.assertEqual(self.client.get(url).status_code,200,url)
 def test_token_revoke(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('root',password='test',is_staff=True,is_superuser=True);self.client.force_login(u)
  r=self.client.post('/tokens/',{'name':'read','can_read':'on'})
  self.assertContains(r,'Copie agora');key=ApiKey.objects.get(name='read')
  self.client.post('/tokens/',{'revoke':key.id});key.refresh_from_db();self.assertFalse(key.enabled)
 def test_operator_cannot_tokens(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('op',password='test',is_staff=True);self.client.force_login(u)
  self.assertEqual(self.client.get('/tokens/').status_code,302)
 def test_exclusion_and_gaps(self):
  self.send('a',10,3);self.c.include_in_total=False;self.c.save()
  self.assertEqual(report(self.day)['totals']['entries'],0)
  d=report(self.day,self.c.id);self.assertEqual(d['totals']['entries'],10);self.assertTrue(d['cameras'][0]['coverage_gaps'])
 def test_group_filter(self):
  from .models import DeviceGroup
  g=DeviceGroup.objects.create(name='Entrada');self.c.group=g;self.c.save();self.send('a',10,3)
  self.assertEqual(report(self.day,group_id=g.id)['totals']['entries'],10)
  self.assertEqual(report(self.day,group_id=999)['totals']['entries'],0)
 def test_report_csv(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('v',password='test');self.client.force_login(u)
  self.send('a',10,3)
  r=self.client.get(f'/reports/?start={self.day}&end={self.day}&format=csv')
  self.assertEqual(r.status_code,200);self.assertIn(b'10;3;7;13',r.content)
 def test_demo_isolated(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('demo',password='test');self.client.force_login(u)
  d=self.client.get('/api/v1/demo/').json()
  self.assertTrue(d['demo']);self.assertEqual(Reading.objects.count(),0)
  self.assertEqual(d['totals']['passages'],sum(x['passages'] for x in d['intervals']))
 def test_viewer_cannot_diagnose(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('view',password='test');self.client.force_login(u)
  self.assertEqual(self.client.get('/diagnostics/').status_code,302)
 def test_offline_and_recovery(self):
  from .operations import monitor_devices
  from .models import DeviceAlert
  now=timezone.now().replace(hour=15,minute=0,second=0)
  monitor_devices(now);monitor_devices(now)
  self.assertEqual(DeviceAlert.objects.filter(resolved_at__isnull=True).count(),1)
  self.c.last_seen=now;self.c.save();monitor_devices(now)
  self.assertEqual(DeviceAlert.objects.filter(resolved_at__isnull=True).count(),0)
 def test_closed_calendar_suppresses(self):
  from .operations import monitor_devices
  from .models import DeviceAlert,CalendarException
  now=timezone.now();CalendarException.objects.create(day=now.astimezone(ZoneInfo('America/Sao_Paulo')).date(),description='Fechado')
  monitor_devices(now);self.assertEqual(DeviceAlert.objects.count(),0)
 def test_ops_pages(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('ops',password='test',is_staff=True,is_superuser=True);self.client.force_login(u)
  for path in ['/calendar/','/events/','/audit/','/alerts/','/summaries/','/api/docs/']:
   self.assertEqual(self.client.get(path).status_code,200,path)
 def test_audit_actor_and_secret(self):
  from django.contrib.auth.models import User
  from .models import AuditLog
  u=User.objects.create_user('auditor',password='test',is_staff=True,is_superuser=True);self.client.force_login(u)
  self.client.post('/tokens/',{'name':'audited','can_read':'on'})
  log=AuditLog.objects.filter(object_type='ApiKey').last()
  self.assertEqual(log.actor,'auditor');self.assertNotIn('digest',log.changes)
 def test_summary_idempotent(self):
  from .operations import generate_summaries
  from .models import ScheduledSummary
  self.send('a',10,3);generate_summaries();n=ScheduledSummary.objects.count();generate_summaries()
  self.assertEqual(ScheduledSummary.objects.count(),n)
  self.assertEqual(ScheduledSummary.objects.get(kind='daily',start=self.day).data['totals']['entries'],10)
 def test_events_and_ranking(self):
  from django.contrib.auth.models import User
  from .models import FlowEvent
  FlowEvent.objects.create(title='Campanha',day=self.day);self.send('a',10,3)
  u=User.objects.create_user('reports',password='test');self.client.force_login(u)
  self.assertContains(self.client.get(f'/reports/?start={self.day}&end={self.day}'),'Campanha')
 def prepare_whatsapp(self):
  from .models import WhatsAppConfig,RawDelivery
  now=datetime.combine(timezone.localdate(),datetime.min.time(),ZoneInfo('America/Sao_Paulo'))+timedelta(hours=22,minutes=10)
  self.c.last_seen=now;self.c.save()
  raw=RawDelivery.objects.create(camera=self.c,payload={})
  Reading.objects.create(camera=self.c,event_id='wa',timestamp=now-timedelta(minutes=1),entries=40,exits=12,mode='delta',raw=raw)
  WhatsAppConfig.objects.create(pk=1,enabled=True,chat_id='5581999999999@c.us')
  return now
 def test_whatsapp_once(self):
  from unittest.mock import patch
  from .whatsapp import scheduled_send
  from .models import WhatsAppDelivery
  now=self.prepare_whatsapp()
  with patch('flow.whatsapp.session_info',return_value={'status':'WORKING'}),patch('flow.whatsapp.gateway',return_value={'id':'abc'}) as send:
   scheduled_send(now);scheduled_send(now)
   self.assertEqual(send.call_count,1)
   self.assertEqual(send.call_args.args[2]['text'],'Boa noite, fluxo final 40')
  self.assertEqual(WhatsAppDelivery.objects.get().status,'accepted')
 def test_whatsapp_unknown_no_retry(self):
  from unittest.mock import patch
  from .whatsapp import scheduled_send
  from .models import WhatsAppDelivery
  now=self.prepare_whatsapp()
  with patch('flow.whatsapp.session_info',return_value={'status':'WORKING'}),patch('flow.whatsapp.gateway',side_effect=TimeoutError) as send:
   scheduled_send(now);scheduled_send(now+timedelta(minutes=1));self.assertEqual(send.call_count,1)
  self.assertEqual(WhatsAppDelivery.objects.get().status,'unknown')
 def test_whatsapp_missing_data(self):
  from unittest.mock import patch
  from .whatsapp import scheduled_send
  from .models import WhatsAppDelivery
  now=self.prepare_whatsapp();Reading.objects.all().delete()
  with patch('flow.whatsapp.session_info',return_value={'status':'WORKING'}),patch('flow.whatsapp.gateway') as send:
   scheduled_send(now);send.assert_not_called()
  self.assertEqual(WhatsAppDelivery.objects.get().status,'skipped')
 def test_whatsapp_time_and_offline(self):
  from unittest.mock import patch
  from .whatsapp import scheduled_send
  from .models import WhatsAppDelivery
  now=self.prepare_whatsapp()
  with patch('flow.whatsapp.session_info',return_value={'status':'STOPPED'}),patch('flow.whatsapp.gateway') as send:
   scheduled_send(now-timedelta(minutes=1));scheduled_send(now);scheduled_send(now+timedelta(minutes=11));send.assert_not_called()
  self.assertEqual(WhatsAppDelivery.objects.count(),0)
 def test_whatsapp_admin_page(self):
  from unittest.mock import patch
  from django.contrib.auth.models import User
  u=User.objects.create_user('waadmin',password='test',is_superuser=True,is_staff=True);self.client.force_login(u)
  with patch('flow.whatsapp_views.session_info',return_value={'status':'STOPPED'}):
   self.assertContains(self.client.get('/whatsapp/'),'22:10')
 def test_whatsapp_form(self):
  from .whatsapp_views import ConfigForm
  form=ConfigForm({'enabled':'on','number':'+55 (81) 99999-9999','metric':'entries','send_at':'22:10'})
  self.assertTrue(form.is_valid(),form.errors);self.assertEqual(form.cleaned_data['chat_id'],'5581999999999@c.us')
 def native_payload(self):
  self.c.serial='201000002212160408';self.c.save()
  ts=int(timezone.now().timestamp())-120
  payload={k:0 for k in ('in','out','passby','turnback','avgStayTime','inAdult','outAdult','passbyAdult','turnbackAdult','inChild','outChild','passbyChild','turnbackChild','total')}
  payload.update(sn=self.c.serial,startTime=ts-60,endTime=ts,time=ts,**{'in':10,'out':3,'total':13})
  return payload
 def native_post(self,p):return self.client.post('/api/camera/dataUpload/test',json.dumps(p),content_type='application/json')
 def test_native_upsert(self):
  p=self.native_payload();r=self.native_post(p);self.assertEqual(r.json()['code'],0)
  self.native_post(p);self.assertEqual(Reading.objects.count(),1)
  p['time']+=1;p['in']=12;self.native_post(p)
  self.assertEqual(Reading.objects.count(),1);self.assertEqual(Reading.objects.get().entries,12)
  day=datetime.fromtimestamp(p['endTime'],ZoneInfo('America/Sao_Paulo')).date()
  self.assertEqual(report(day)['totals']['entries'],12)
 def test_native_stale(self):
  p=self.native_payload();self.native_post(p);p['time']-=1;p['in']=100
  self.assertEqual(self.native_post(p).json()['code'],0)
  self.assertEqual(Reading.objects.get().entries,10)
 def test_native_heartbeat_clock(self):
  p=self.native_payload()
  r=self.client.post('/api/camera/heartBeat/test',json.dumps({'sn':p['sn'],'time':p['time']}),content_type='application/json')
  self.assertEqual(r.json()['code'],0);self.assertAlmostEqual(r.json()['data']['time'],int(timezone.now().timestamp()),delta=2)
  self.assertEqual(Reading.objects.count(),0)
 def test_native_unknown_serial(self):
  p=self.native_payload();p['sn']='unknown'
  self.assertEqual(self.native_post(p).json()['code'],1);self.assertEqual(Reading.objects.count(),0)
 def test_native_overlap_reject(self):
  p=self.native_payload();self.native_post(p)
  p['endTime']+=30;p['time']+=30
  self.assertEqual(self.native_post(p).status_code,400);self.assertEqual(Reading.objects.count(),1)
 def test_native_signature_reference(self):
  from .native import sign_parameters
  self.assertEqual(sign_parameters({'startIndex':0,'getQuantity':100},'test-secret'),hashlib.md5(b'getQuantity=100&startIndex=0&test-secret').hexdigest().upper())
  self.assertEqual(sign_parameters({},'test-secret'),hashlib.md5(b'test-secret').hexdigest().upper())
 def test_native_malformed(self):
  p=self.native_payload();p['in']=-1
  self.assertEqual(self.native_post(p).status_code,400);self.assertEqual(Reading.objects.count(),0)
 def test_native_scoped(self):
  p=self.native_payload();other=Camera.objects.create(name='Outro',ip='192.168.1.236',serial='another')
  p['sn']=other.serial;self.assertEqual(self.native_post(p).status_code,403)
 def test_device_token_generation(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('tokenadmin',password='test',is_superuser=True,is_staff=True);self.client.force_login(u)
  self.c.serial='serial-one';self.c.save()
  url=f'/devices/{self.c.id}/token/'
  self.assertEqual(self.client.get(url).status_code,405)
  r=self.client.post(url);self.assertContains(r,'/api/camera/dataUpload/')
  k=ApiKey.objects.get(name=f'push-device-{self.c.id}')
  self.assertTrue(k.can_ingest);self.assertFalse(k.can_read);self.assertEqual(k.camera_id,self.c.id)
  self.client.post(url);k.refresh_from_db();self.assertFalse(k.enabled)
  self.assertEqual(ApiKey.objects.filter(name=k.name,enabled=True).count(),1)
  self.assertTrue(ApiKey.objects.get(name='test').enabled)
 def test_device_token_needs_serial(self):
  from django.contrib.auth.models import User
  u=User.objects.create_user('serialadmin',password='test',is_superuser=True,is_staff=True);self.client.force_login(u)
  self.assertEqual(self.client.post(f'/devices/{self.c.id}/token/').status_code,400)
