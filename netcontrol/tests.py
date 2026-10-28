from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import NetworkAsset, Site


class NetworkAssetCreateTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name='Unidade de teste')
        self.user = get_user_model().objects.create_user(
            username='netcontrol-staff', password='test-password', is_staff=True
        )

    def test_create_page_requires_staff_login(self):
        response = self.client.get(reverse('netcontrol-asset-create'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_staff_can_create_asset_with_environment_secret_reference(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('netcontrol-asset-create'), {
            'name': 'Switch teste', 'kind': 'switch', 'site': self.site.pk,
            'ip': '192.0.2.10', 'protocol': 'routeros', 'username': 'monitor',
            'credential_env': 'NETCONTROL_SWITCH_TEST_SECRET', 'verify_tls': 'on',
        })
        self.assertRedirects(response, reverse('netcontrol-dashboard'))
        asset = NetworkAsset.objects.get(name='Switch teste')
        self.assertEqual(asset.credential_env, 'NETCONTROL_SWITCH_TEST_SECRET')
        self.assertEqual(asset.last_status, 'unknown')
        self.assertFalse(asset.enabled)

    def test_secret_value_is_not_a_form_field(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('netcontrol-asset-create'))
        self.assertNotContains(response, 'name="password"')
        self.assertContains(response, 'não cole senhas')
