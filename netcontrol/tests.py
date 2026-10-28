from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from unittest.mock import patch
import os

from .models import NetworkAsset, Site
from .management.commands.poll_network import poll_snmp_asset, Command
from .snmp import SnmpPollError, collect_snmp, parse_interface_table


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


class SnmpCollectionTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name='Unidade SNMP')
        self.asset = NetworkAsset.objects.create(
            name='Switch Intelbras', kind='switch', site=self.site,
            ip='192.168.0.2', protocol='snmp', credential_env='CRED_SG2',
            enabled=True,
        )

    @patch('netcontrol.management.commands.poll_network.collect_snmp')
    def test_snmp_poll_updates_asset_interfaces_and_history(self, collect):
        collect.return_value = {
            'system_description': 'Intelbras SG 2404 PoE',
            'interfaces': [
                {'index': 1, 'name': 'GigabitEthernet1/0/1', 'status': 'online',
                 'speed_bps': 1000000000, 'mac': '58:10:8C:74:C3:E1'},
                {'index': 2, 'name': 'GigabitEthernet1/0/2', 'status': 'offline',
                 'speed_bps': 1000000000, 'mac': '58:10:8C:74:C3:E2'},
            ],
        }
        with patch.dict(os.environ, {'CRED_SG2': 'test-community-do-not-log'}):
            Command().handle(asset=self.asset.pk)

        self.asset.refresh_from_db()
        self.assertEqual(self.asset.last_status, 'online')
        self.assertIsNotNone(self.asset.last_checked)
        self.assertEqual(self.asset.interfaces.count(), 2)
        self.assertEqual(self.asset.interfaces.get(index=1).status, 'online')
        self.assertEqual(self.asset.interfaces.get(index=2).status, 'offline')
        self.assertTrue(self.asset.checks.latest('created_at').successful)
        self.assertNotIn('test-community-do-not-log', self.asset.checks.latest('created_at').detail)

    def test_missing_environment_community_fails_without_network_request(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CRED_SG2', None)
            with patch('netcontrol.management.commands.poll_network.collect_snmp') as collect:
                with self.assertRaisesMessage(ValueError, 'não disponível no coletor'):
                    poll_snmp_asset(self.asset)
                collect.assert_not_called()

    def test_snmp_collection_rejects_public_addresses(self):
        with self.assertRaises(SnmpPollError):
            collect_snmp('8.8.8.8', 'community-is-never-logged')

    def test_interface_table_parser_reads_port_name_status_speed_and_mac(self):
        class Value:
            def __init__(self, value): self.value = value
            def prettyPrint(self): return str(self.value)
            def __int__(self): return int(self.value)
            def asOctets(self): return bytes.fromhex(self.value)

        result = parse_interface_table([
            ('1.3.6.1.2.1.2.2.1.2.1', Value('GigabitEthernet1/0/1')),
            ('1.3.6.1.2.1.2.2.1.5.1', Value(1000000000)),
            ('1.3.6.1.2.1.2.2.1.6.1', Value('58108c74c3e1')),
            ('1.3.6.1.2.1.2.2.1.8.1', Value(1)),
            ('1.3.6.1.2.1.2.2.1.2.2', Value('GigabitEthernet1/0/2')),
            ('1.3.6.1.2.1.2.2.1.8.2', Value(2)),
            ('1.3.6.1.2.1.1.1.0', Value('not-an-ifTable-object')),
        ])
        self.assertEqual(result, [
            {'index': 1, 'name': 'GigabitEthernet1/0/1', 'speed_bps': 1000000000,
             'mac': '58:10:8C:74:C3:E1', 'status': 'online'},
            {'index': 2, 'name': 'GigabitEthernet1/0/2', 'status': 'offline'},
        ])
