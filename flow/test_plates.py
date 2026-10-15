import hashlib
import io
import uuid
from PIL import Image
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client
from django.utils import timezone
from .models import PlateCamera, PlateCapture

class PlateTests(TestCase):
    def setUp(self):
        self.camera=PlateCamera.objects.create(name='Entrada 1', token_digest=hashlib.sha256(b'test-secret').hexdigest())
        self.user=User.objects.create_user('viewer',password='test-password')
    def payload(self, **overrides):
        image=io.BytesIO(); Image.new('RGB',(120,40)).save(image,'JPEG')
        data={'event_id':str(uuid.uuid4()),'plate':'ABC1D23','captured_at':timezone.now().isoformat(),'confidence':'0.91','image':SimpleUploadedFile('photo.jpg',image.getvalue(),'image/jpeg')}
        data.update(overrides); return data
    def send(self, data, token='test-secret'):
        return self.client.post('/api/v1/plates/ingest/',data,HTTP_AUTHORIZATION='Bearer '+token)
    def test_auth_disabled_and_invalid_inputs(self):
        self.assertEqual(self.send(self.payload(),token='bad').status_code,401)
        for values in ({'plate':'WRONG'},{'confidence':'nan'},{'confidence':'1.2'},{'captured_at':'2026-10-15T12:00:00'},{'event_id':'oops'},{'image':SimpleUploadedFile('fake.jpg',b'not an image')}):
            self.assertEqual(self.send(self.payload(**values)).status_code,400)
        self.camera.enabled=False; self.camera.save()
        self.assertEqual(self.send(self.payload()).status_code,401)
    def test_idempotency_and_camera_scoping(self):
        event=str(uuid.uuid4())
        self.assertEqual(self.send(self.payload(event_id=event)).status_code,201)
        self.assertEqual(self.send(self.payload(event_id=event)).status_code,200)
        second=PlateCamera.objects.create(name='Entrada 2',token_digest=hashlib.sha256(b'second').hexdigest())
        self.assertEqual(self.send(self.payload(event_id=event),token='second').status_code,201)
        self.assertEqual(PlateCapture.objects.count(),2)
    def test_private_photo_and_readonly_user(self):
        self.send(self.payload()); obj=PlateCapture.objects.first()
        self.assertEqual(self.client.get(f'/plates/{obj.pk}/photo/').status_code,302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/plates/').status_code,200)
        self.assertEqual(self.client.get('/plates/?start=invalid').status_code,200)
        self.assertEqual(self.client.get(f'/plates/{obj.pk}/photo/')['Cache-Control'],'private, no-store')
        self.assertEqual(self.client.post(f'/plates/{obj.pk}/review/',{'plate':'XYZ1234'}).status_code,302)
        obj.refresh_from_db(); self.assertEqual(obj.plate,'ABC1D23')
        self.assertEqual(self.client.get('/plates/cameras/').status_code,302)
    def test_staff_review_csrf_and_filters(self):
        self.send(self.payload()); obj=PlateCapture.objects.first()
        self.user.is_staff=True; self.user.save(); self.client.force_login(self.user)
        csrf=Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post(f'/plates/{obj.pk}/review/',{'plate':'XYZ1234'}).status_code,403)
        self.assertEqual(self.client.post(f'/plates/{obj.pk}/review/',{'plate':'XYZ-1234'}).status_code,302)
        obj.refresh_from_db(); self.assertEqual(obj.plate,'XYZ1234'); self.assertTrue(obj.reviewed)
        self.assertContains(self.client.get('/plates/?plate=XYZ'),'XYZ1234')
        self.assertNotContains(self.client.get('/plates/?plate=ABC'),'XYZ1234')
