import os
from unittest.mock import patch
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Camera,PullCursor,CommunicationCheck,Reading,CollectionGap
from .pfc_pull import ProtocolError
from .management.commands.poll_pfc import dispatch_ready,collect_group

class DeviceCommunicationTests(TestCase):
    def setUp(self):
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        self.operator=User.objects.create_user('operator',is_staff=True)
        self.client.force_login(self.operator)
        self.url=f'/devices/{self.camera.pk}/communication/'
    @patch('flow.device_communication.fetch',return_value=[])
    @patch('flow.device_communication.socket.create_connection')
    @patch.dict(os.environ,{'PFC_SERVICE_SECRET':'test','PFC_PULL_IPS':'192.168.1.235'})
    def test_signed_probe_never_advances_counts_or_marks_gap_recovered(self,connect,fetch):
        state=PullCursor.objects.create(camera=self.camera,initialized=True,offset=100,last_success=timezone.now())
        gap=CollectionGap.objects.create(camera=self.camera,started_at=timezone.now())
        response=self.client.post(self.url)
        self.assertContains(response,'O equipamento está respondendo à API')
        self.assertContains(response,'O equipamento pode responder e o envio continuar aguardando reexame.')
        fetch.assert_called_once_with(self.camera,100,1,timeout=4)
        state.refresh_from_db();gap.refresh_from_db();self.camera.refresh_from_db()
        self.assertEqual(state.offset,100);self.assertEqual(gap.state,'open');self.assertIsNone(self.camera.last_seen);self.assertFalse(Reading.objects.exists())
        self.client.post(self.url);fetch.assert_called_once()
    @patch('flow.device_communication.fetch')
    @patch('flow.device_communication.socket.create_connection',side_effect=TimeoutError)
    def test_timeout_shows_route_firewall_possibilities_without_guessing(self,connect,fetch):
        self.assertContains(self.client.post(self.url),'Possíveis causas: equipamento desligado, rota/VLAN ou bloqueio no firewall.')
        fetch.assert_not_called();self.assertEqual(CommunicationCheck.objects.get().result['state'],'offline')
    @patch.dict(os.environ,{'PFC_SERVICE_SECRET':'example-secret'})
    @patch('flow.device_communication.fetch',side_effect=ProtocolError('json sign err example-secret'))
    @patch('flow.device_communication.socket.create_connection')
    def test_auth_problem_shows_action_without_secret_or_payload(self,connect,fetch):
        response=self.client.post(self.url)
        self.assertContains(response,'Assinatura/chave recusada');self.assertNotContains(response,'example-secret')
    @patch.dict(os.environ,{'PFC_SERVICE_SECRET':'test'})
    @patch('flow.device_communication.fetch',return_value=[])
    @patch('flow.device_communication.socket.create_connection')
    def test_replicas_share_one_probe_per_physical_ip(self,connect,fetch):
        source=Camera.objects.create(name='Origem',ip='192.168.1.237')
        target=Camera.objects.create(name='Replica',ip=source.ip)
        self.client.post(f'/devices/{source.pk}/communication/')
        self.assertContains(self.client.post(f'/devices/{target.pk}/communication/'),'Já existe um teste recente')
        self.assertEqual(fetch.call_count,1);self.assertEqual(CommunicationCheck.objects.count(),1)
    @patch('flow.device_communication.socket.create_connection')
    def test_get_is_read_only_and_only_staff_can_start_checks(self,connect):
        self.assertContains(self.client.get(self.url),'Verificar comunicação agora');connect.assert_not_called();self.assertFalse(CommunicationCheck.objects.exists())
        self.client.force_login(User.objects.create_user('viewer'))
        self.assertEqual(self.client.post(self.url).status_code,302);connect.assert_not_called()
        self.client.force_login(self.operator)
        self.camera.ip='169.254.169.254';self.camera.save()
        self.assertContains(self.client.post(self.url),'Endereço fora da rede privada suportada');connect.assert_not_called()
    @patch('flow.management.commands.poll_pfc.observe_collection',return_value=False)
    @patch('flow.management.commands.poll_pfc.poll',return_value=0)
    @patch('flow.management.commands.poll_pfc.mirror',return_value=0)
    def test_group_polls_hardware_once_and_mirrors_after_source(self,mirror,poll,observe):
        source=Camera.objects.create(name='Source',ip='192.168.1.237')
        target=Camera.objects.create(name='Replica',ip=source.ip)
        result=collect_group([target,source])
        poll.assert_called_once_with(source);mirror.assert_called_once_with(source,target);self.assertTrue(result['success'])
    @patch('flow.management.commands.poll_pfc.collect_ip')
    def test_fast_ip_repeats_while_slow_ip_is_still_waiting(self,collect):
        entered=Event();release=Event();fast_done=Event();count={'fast':0,'slow':0}
        def worker(ip):
            count[ip]+=1
            if ip=='slow':entered.set();release.wait(3)
            else:fast_done.set()
            return {'success':True,'resumed':False}
        collect.side_effect=worker
        running={};ready={'slow':0,'fast':0};started={}
        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                dispatch_ready(pool,running,ready,['slow','fast'],0,3,started)
                self.assertTrue(entered.wait(1));self.assertTrue(fast_done.wait(1));running['fast'].result(timeout=1)
                fast_done.clear()
                dispatch_ready(pool,running,ready,['slow','fast'],3,3,started)
                self.assertTrue(fast_done.wait(1));self.assertFalse(running['slow'].done())
                self.assertEqual(count,{'slow':1,'fast':2})
            finally:release.set()
