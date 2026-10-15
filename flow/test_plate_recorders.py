import hashlib
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from django.db import IntegrityError,transaction
from django.test import TestCase,Client
from .models import PlateRecorder,PlateCamera,PlateCapture
from . import test_plates
from .plate_recorders import ChannelForm
from collector.nvr import rtsp_url,reconcile
from pathlib import Path

class NVRTests(TestCase):
    def setUp(self):
        self.nvr=PlateRecorder.objects.create(name='Estacionamento',host='192.168.1.49',token_digest=hashlib.sha256(b'nvr-token').hexdigest())
        self.channel=PlateCamera.objects.create(name='Entrada',recorder=self.nvr,channel=1,roi=[.5,.6,.2,.1],token_digest=hashlib.sha256(b'camera-token').hexdigest())
        self.admin=User.objects.create_superuser('admin','admin@example.test','test-password')
    def config(self,token='nvr-token'):
        return self.client.get('/api/v1/plates/collector-config/',HTTP_AUTHORIZATION='Bearer '+token)
    def channel_form(self,enabled=(1,)):
        result={}
        for n in range(1,17):
            for k,v in {'name':f'Canal {n}','stream':'0','x':'0.5','y':'0.6','width':'0.2','height':'0.1'}.items():result[f'ch{n}-{k}']=v
            if n in enabled:result[f'ch{n}-enabled']='on'
        return result
    def test_configuration_scope_and_disabled_recorder(self):
        other=PlateRecorder.objects.create(name='Outro',host='192.168.1.50',token_digest=hashlib.sha256(b'other').hexdigest())
        PlateCamera.objects.create(name='Outra',recorder=other,channel=2,token_digest='f'*64)
        data=self.config().json(); self.assertEqual([c['channel'] for c in data['channels']],[1]); self.assertNotIn('token_digest',str(data));self.assertNotIn('password',str(data))
        self.assertEqual(self.config('camera-token').status_code,401)
        self.nvr.refresh_from_db();self.assertIsNotNone(self.nvr.last_seen)
        self.nvr.enabled=False;self.nvr.save();self.assertEqual(self.config().json()['channels'],[])
    def test_channels_selection_preserves_history_and_validates_roi(self):
        self.client.force_login(self.admin)
        url=f'/plates/nvrs/{self.nvr.pk}/channels/'
        self.assertEqual(self.client.post(url,self.channel_form((2,16))).status_code,302)
        self.channel.refresh_from_db();self.assertFalse(self.channel.enabled)
        self.assertEqual(set(PlateCamera.objects.filter(enabled=True).values_list('channel',flat=True)),{2,16})
        data=self.channel_form((1,));data['ch1-width']='0.9'
        self.assertEqual(self.client.post(url,data).status_code,200)
        self.channel.refresh_from_db();self.assertFalse(self.channel.enabled)
    def test_permissions_csrf_and_token_rotation(self):
        viewer=User.objects.create_user('viewer',password='test-password');self.client.force_login(viewer)
        self.assertEqual(self.client.get('/plates/nvrs/').status_code,302)
        self.client.force_login(self.admin)
        url=f'/plates/nvrs/{self.nvr.pk}/edit/'
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.admin)
        self.assertEqual(csrf.post(url,{'action':'rotate'}).status_code,403)
        response=self.client.post(url,{'action':'rotate'})
        self.assertEqual(response.status_code,200);self.assertEqual(response['Cache-Control'],'private, no-store')
        self.assertEqual(self.config().status_code,401)
        self.assertEqual(self.config(response.context['token']).status_code,200)
    def test_channel_bounds_constraint(self):
        with self.assertRaises(IntegrityError),transaction.atomic():
            PlateCamera.objects.create(name='Invalid',recorder=self.nvr,channel=17,token_digest='a'*64)
        with self.assertRaises(IntegrityError),transaction.atomic():
            PlateCamera.objects.create(name='Null',recorder=self.nvr,channel=None,token_digest='b'*64)
    def test_rtsp_credentials_escaped(self):
        self.assertEqual(rtsp_url({'host':'192.168.1.49','port':554},{'channel':16,'stream':0},'user@name','pass:#?'), 'rtsp://user%40name:pass%3A%23%3F@192.168.1.49:554/cam/realmonitor?channel=16&subtype=0')
    @patch('collector.nvr.subprocess.Popen')
    def test_supervisor_starts_selected_stops_removed(self,popen):
        process=Mock();process.poll.return_value=None;popen.return_value=process
        children={};nvr={'id':1,'host':'192.168.1.49','port':554,'enabled':True}
        reconcile(children,[{'channel':2,'stream':0,'roi':[0,0,1,1]}],nvr,'https://example.test','token','user','password',Path('/tmp/queue'))
        self.assertEqual(set(children),{2});self.assertEqual(popen.call_args.kwargs['env']['PLATES_CHANNEL'],'2')
        reconcile(children,[],nvr,'https://example.test','token','user','password',Path('/tmp/queue'))
        self.assertEqual(children,{});process.terminate.assert_called_once()

class NVRIngestTests(TestCase):
    setUp=test_plates.PlateTests.setUp
    payload=test_plates.PlateTests.payload
    send=test_plates.PlateTests.send
    def test_nvr_ingest_scope_and_disabled(self):
        nvr=PlateRecorder.objects.create(name='NVR',host='192.168.1.49',token_digest=hashlib.sha256(b'nvr').hexdigest())
        self.camera.recorder=nvr;self.camera.channel=3;self.camera.save()
        self.assertEqual(self.send(self.payload(channel='3'),token='nvr').status_code,201)
        self.assertEqual(PlateCapture.objects.get().camera,self.camera)
        self.assertEqual(self.send(self.payload(channel='1'),token='nvr').status_code,401)
        self.assertEqual(self.send(self.payload(),token='nvr').status_code,400)
        nvr.enabled=False;nvr.save()
        self.assertEqual(self.send(self.payload(channel='3'),token='nvr').status_code,401)
        self.assertEqual(self.send(self.payload(),token='test-secret').status_code,401)
