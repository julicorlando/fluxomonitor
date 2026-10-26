import io
from tempfile import TemporaryDirectory
from datetime import date
from PIL import Image
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from .models import Asset, AssetAttachment, Company, Contact
from .forms import AssetForm

class AssetFilesTests(TestCase):
    def setUp(self):
        self.storage = TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=self.storage.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.storage.cleanup)
        self.company = Company.objects.get(code='SC')
        self.other = Company.objects.get(code='PDU')
        self.root = get_user_model().objects.create_superuser(username='inventoryroot', password='example')
        self.asset = Asset.objects.create(company=self.company, tag='PAT-001', name='Notebook', invoice_number='NF-9988', acquisition_date=date(2026,10,20))
        self.client.force_login(self.root)

    def test_new_requires_fiscal_fields_and_patrimony(self):
        form = AssetForm({'company':self.company.pk,'name':'Notebook','kind':'computer','state':'active'}, companies=Company.objects.all())
        self.assertFalse(form.is_valid())
        for field in ('tag', 'invoice_number', 'acquisition_date'):
            self.assertIn(field, form.errors)

    def test_photo_is_compressed_and_access_limited(self):
        raw = io.BytesIO()
        Image.new('RGB', (3000, 2000), (110, 120, 130)).save(raw, 'PNG')
        image = SimpleUploadedFile('asset.png', raw.getvalue(), content_type='image/png')
        response = self.client.post(f'/support/assets/{self.asset.pk}/', {'action':'attachment','kind':'photo','file':image})
        self.assertEqual(response.status_code, 302)
        attached = AssetAttachment.objects.get()
        self.assertTrue(attached.file.name.endswith('.webp'))
        self.assertLess(attached.size_bytes, 500*1024)
        response = self.client.get(f'/support/assets/attachments/{attached.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/webp')
        self.client.logout()
        response = self.client.get(f'/support/assets/attachments/{attached.pk}/')
        self.assertNotEqual(response.status_code, 200)

    def test_terms_require_pdf_signature(self):
        bad = SimpleUploadedFile('term.pdf', b'not really a pdf', content_type='application/pdf')
        resp = self.client.post(f'/support/assets/{self.asset.pk}/', {'action':'attachment','kind':'signed_term','file':bad})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AssetAttachment.objects.exists())
        good = SimpleUploadedFile('signed.pdf', b'%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF',content_type='application/pdf')
        resp = self.client.post(f'/support/assets/{self.asset.pk}/', {'action':'attachment','kind':'signed_term','file':good})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(AssetAttachment.objects.get().kind, 'signed_term')
