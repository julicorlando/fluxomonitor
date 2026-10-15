import hashlib
from datetime import timedelta
from unittest.mock import patch,Mock
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from .models import PlateRecorder,PlateCamera
from .plate_status import recorder_status,camera_status
from collector.plates import video_report

class VideoStatusTests(TestCase):
    def setUp(self):
        self.nvr=PlateRecorder.objects.create(name='NVR',host='192.168.1.8',integrated_enabled=False,token_digest=hashlib.sha256(b'nvr-token').hexdigest())
        self.camera=PlateCamera.objects.create(name='Entrada',recorder=self.nvr,channel=1,token_digest=hashlib.sha256(b'camera-token').hexdigest())
    def heartbeat(self,age='',channel=1,token='nvr-token'):
        return self.client.post('/api/v1/plates/heartbeat/',{'frame_age':age,'channel':channel},HTTP_AUTHORIZATION='Bearer '+token)
    def test_fresh_video_without_plate_capture(self):
        self.assertEqual(self.heartbeat('0.5').status_code,200)
        self.camera.refresh_from_db()
        self.assertEqual(camera_status(self.camera)['state'],'streaming')
        self.assertIsNotNone(self.camera.last_frame_at)
        self.assertIsNone(self.camera.last_seen)
        self.assertEqual(recorder_status(self.nvr)['state'],'streaming')
    def test_control_contact_is_not_video_connection(self):
        self.nvr.last_seen=timezone.now();self.nvr.save()
        self.assertEqual(recorder_status(self.nvr)['state'],'pending')
        self.assertEqual(recorder_status(self.nvr)['collector'],'Conectado ao painel')
        self.heartbeat('');self.camera.refresh_from_db()
        self.assertEqual(camera_status(self.camera)['state'],'unavailable')
    def test_silent_collector_expires_and_partial_channels(self):
        now=timezone.now()
        self.camera.video_status='streaming';self.camera.video_checked_at=now-timedelta(seconds=46);self.camera.last_frame_at=now-timedelta(seconds=46);self.camera.save()
        self.assertEqual(camera_status(self.camera)['state'],'stale')
        self.heartbeat(1)
        PlateCamera.objects.create(name='Sem vídeo',recorder=self.nvr,channel=2,token_digest='a'*64)
        self.assertEqual(recorder_status(self.nvr)['state'],'partial')
        self.camera.refresh_from_db();self.camera.enabled=False;self.camera.save()
        self.assertEqual(camera_status(self.camera)['state'],'disabled')
    def test_auth_channel_and_values(self):
        self.assertEqual(self.heartbeat(1,token='wrong').status_code,401)
        self.assertEqual(self.heartbeat(1,channel=2).status_code,401)
        for value in ('nan','inf','-1','86401','abc'):
            self.assertEqual(self.heartbeat(value).status_code,400)
        self.nvr.enabled=False;self.nvr.save()
        self.assertEqual(self.heartbeat(1,token='camera-token').status_code,401)
    def test_status_endpoint_protected_and_templates_render(self):
        self.assertEqual(self.client.get('/plates/nvrs/status/').status_code,302)
        admin=User.objects.create_superuser('admin','admin@example.test','test-password')
        self.client.force_login(admin)
        self.assertContains(self.client.get('/plates/nvrs/'),'Aguardando teste de vídeo pelo coletor')
        self.assertContains(self.client.get(f'/plates/nvrs/{self.nvr.pk}/channels/'),'Último quadro')
        response=self.client.get('/plates/nvrs/status/')
        self.assertEqual(response['Cache-Control'],'private, no-store')
        self.assertNotIn('token_digest',str(response.json()))
        self.assertEqual(response.json()['recorders'][0]['channels'][0]['state'],'pending')
    @patch('collector.plates.time.monotonic',return_value=100)
    def test_report_uses_received_frame_age(self,clock):
        video=Mock();video.get.return_value=(None,0,0)
        self.assertEqual(video_report(video),{'frame_age':''})
        video.get.return_value=(object(),99,10)
        self.assertEqual(video_report(video),{'frame_age':1})
        video.get.return_value=(object(),50,1)
        self.assertEqual(video_report(video),{'frame_age':50})
