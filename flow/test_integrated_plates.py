import hashlib
import sqlite3
import uuid
from unittest.mock import Mock,patch
from cryptography.fernet import InvalidToken
from django.contrib.auth.models import User
from django.test import TestCase,override_settings
from django.utils import timezone
from .models import PlateRecorder,PlateCamera,PlateCapture,AuditLog
from .plate_credentials import encrypt_password,decrypt_password
from .plate_worker import channel_plan,sync_children,save_pending
from .plate_recorders import RecorderForm
from .plate_status import recorder_status

class IntegratedPlateTests(TestCase):
    def setUp(self):
        self.nvr=PlateRecorder.objects.create(name='NVR',host='192.168.1.8',token_digest=hashlib.sha256(b'token').hexdigest())
        self.camera=PlateCamera.objects.create(name='Entrada',recorder=self.nvr,channel=1,token_digest='c'*64)
        self.admin=User.objects.create_superuser('admin','admin@example.test','test-password')
        self.client.force_login(self.admin)
    def configure(self):
        self.nvr.username='reader';self.nvr.password_encrypted=encrypt_password('a-secret-password');self.nvr.save()
    def test_credentials_encrypted_and_not_disclosed(self):
        response=self.client.post(f'/plates/nvrs/{self.nvr.pk}/edit/',{'name':self.nvr.name,'host':self.nvr.host,'port':554,'username':'reader','password':'a-secret-password','enabled':'on','integrated_enabled':'on'})
        self.assertEqual(response.status_code,302)
        self.nvr.refresh_from_db();self.assertNotIn('a-secret-password',self.nvr.password_encrypted)
        self.assertEqual(decrypt_password(self.nvr.password_encrypted),'a-secret-password')
        self.assertNotContains(self.client.get(f'/plates/nvrs/{self.nvr.pk}/edit/'),'a-secret-password')
        self.assertNotIn('a-secret-password',str(list(AuditLog.objects.values('changes'))))
        response=self.client.get('/api/v1/plates/collector-config/',HTTP_AUTHORIZATION='Bearer token')
        self.assertNotIn('password',str(response.json()));self.assertNotIn('reader',str(response.json()))
        self.assertEqual(response.json()['channels'],[]) # Avoid running external and integrated worker together.
    def test_blank_password_keeps_existing_secret(self):
        self.configure();saved=self.nvr.password_encrypted
        self.client.post(f'/plates/nvrs/{self.nvr.pk}/edit/',{'name':self.nvr.name,'host':self.nvr.host,'port':554,'username':'reader','password':'','enabled':'on','integrated_enabled':'on'})
        self.nvr.refresh_from_db();self.assertEqual(self.nvr.password_encrypted,saved)
        form=RecorderForm({'name':'Test','host':'192.168.1.8','port':554,'username':'reader','integrated_enabled':'on','enabled':'on'})
        self.assertFalse(form.is_valid())
    def test_worker_plan_requires_enabled_channels_and_credentials(self):
        self.assertEqual(channel_plan(),{})
        self.assertIn('Configure o usuário',recorder_status(self.nvr)['label'])
        self.configure();first=channel_plan();self.assertEqual(set(first),{self.camera.pk})
        self.nvr.password_encrypted=encrypt_password('updated-secret');self.nvr.save()
        self.assertNotEqual(first[self.camera.pk],channel_plan()[self.camera.pk])
        self.camera.enabled=False;self.camera.save();self.assertEqual(channel_plan(),{})
        self.camera.enabled=True;self.camera.save();self.nvr.integrated_enabled=False;self.nvr.save();self.assertEqual(channel_plan(),{})
    @patch('flow.plate_worker.os.killpg')
    @patch('flow.plate_worker.subprocess.Popen')
    def test_supervisor_lifecycle_and_no_password_in_process_args(self,popen,killpg):
        process=Mock();process.poll.return_value=None;popen.return_value=process;children={}
        self.configure();plan=channel_plan();sync_children(children,plan)
        args=popen.call_args.args[0]
        self.assertNotIn('a-secret-password',str(args));self.assertIn('read_plate_channel',args)
        sync_children(children,plan);self.assertEqual(popen.call_count,1)
        sync_children(children,{});self.assertEqual(killpg.call_count,2);self.assertEqual(children,{})
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
    def test_spool_idempotency_and_disabled_preservation(self):
        self.configure();db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE queue (event TEXT PRIMARY KEY,plate TEXT,captured TEXT,confidence REAL,image BLOB)')
        event=str(uuid.uuid4());record=(event,'ABC1D23',timezone.now().isoformat(),.9,b'jpeg')
        db.execute('INSERT INTO queue VALUES (?,?,?,?,?)',record);save_pending(db,self.camera.pk)
        self.assertEqual(PlateCapture.objects.count(),1)
        db.execute('INSERT INTO queue VALUES (?,?,?,?,?)',record);save_pending(db,self.camera.pk)
        self.assertEqual(PlateCapture.objects.count(),1)
        self.camera.enabled=False;self.camera.save()
        db.execute('INSERT INTO queue VALUES (?,?,?,?,?)',(str(uuid.uuid4()),*record[1:]));save_pending(db,self.camera.pk)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM queue').fetchone()[0],1)
        db.close()
    def test_encryption_key_rotation_and_wrong_key(self):
        with override_settings(SECRET_KEY='old',SECRET_KEY_FALLBACKS=[]):encrypted=encrypt_password('secret')
        with override_settings(SECRET_KEY='new',SECRET_KEY_FALLBACKS=['old']):self.assertEqual(decrypt_password(encrypted),'secret')
        with override_settings(SECRET_KEY='wrong',SECRET_KEY_FALLBACKS=[]):
            with self.assertRaises(InvalidToken):decrypt_password(encrypted)
