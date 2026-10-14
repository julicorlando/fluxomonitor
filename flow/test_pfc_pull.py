import os
from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from flow.models import Camera,Reading,PullCursor,RawDelivery
from flow.pfc_pull import signature,poll,locate_today,ProtocolError

class PullTests(TestCase):
    def setUp(self):self.c=Camera.objects.create(name='PFC',ip='192.168.1.235')
    def event(self,i,t=0,kind=0):return {'idIndex':i,'timestamp':int((timezone.now()-timedelta(seconds=20-t)).timestamp()),'eventType':kind,'stayTime':100}
    def test_signature_verified_formula(self):
        import hashlib
        self.assertEqual(signature({'startIndex':0,'getQuantity':10},'abc'),hashlib.md5(b'getQuantity10startIndex0abc').hexdigest().upper())
    @patch('flow.pfc_pull.fetch')
    @patch('flow.pfc_pull.locate_today',return_value=(0,'offset'))
    def test_entries_exits_unknown_and_replay(self,locate,fetch):
        events=[self.event(1,kind=0),self.event(2,kind=1),self.event(3,kind=4)]
        fetch.return_value=events
        self.assertEqual(poll(self.c),3)
        self.assertEqual(Reading.objects.count(),2)
        self.assertEqual(sum(Reading.objects.values_list('entries',flat=True)),1)
        self.assertEqual(sum(Reading.objects.values_list('exits',flat=True)),1)
        # Simulate offset changed by device buffer removal: re-discover today.
        fetch.side_effect=[[],events]
        poll(self.c)
        self.assertEqual(Reading.objects.count(),2)
        self.assertEqual(PullCursor.objects.get(camera=self.c).offset,3)
    @patch('flow.pfc_pull.fetch')
    def test_find_today_without_importing_history(self,fetch):
        old=int((timezone.now()-timedelta(days=3)).timestamp())
        events=[dict(idIndex=i,timestamp=old,eventType=0) for i in range(100)] + [self.event(100)]
        fetch.side_effect=lambda c,o,q:events[o:o+q]
        self.assertEqual(locate_today(self.c),(100,"offset"))
        self.assertEqual(Reading.objects.count(),0)
    @patch('flow.pfc_pull.fetch')
    def test_incompatible_pagination_blocks(self,fetch):
        fetch.side_effect=[[self.event(1),self.event(2)],[self.event(50)],[self.event(50)]]
        with self.assertRaises(ProtocolError):locate_today(self.c)
    @patch('flow.pfc_pull.fetch')
    @patch('flow.pfc_pull.locate_today',return_value=(0,'offset'))
    def test_future_clock_does_not_advance(self,locate,fetch):
        fetch.return_value=[self.event(1,t=1000)]
        with self.assertRaises(ProtocolError):poll(self.c)
        self.assertFalse(PullCursor.objects.exists())
        self.assertFalse(RawDelivery.objects.exists())
    def test_other_source_blocks(self):
        raw=RawDelivery.objects.create(camera=self.c,payload={})
        Reading.objects.create(camera=self.c,event_id='push:1',timestamp=timezone.now(),entries=1,exits=0,mode='delta',raw=raw)
        with self.assertRaises(ProtocolError):poll(self.c)

    @patch('flow.pfc_pull.fetch')
    def test_absolute_ids_and_gaps(self,fetch):
        events=[self.event(4739864),self.event(4739868)]
        fetch.side_effect=lambda c,o,q:[e for e in events if e['idIndex']>=o][:q]
        self.assertEqual(locate_today(self.c),(4739864,'id'))
        self.assertEqual(poll(self.c),2)
        state=PullCursor.objects.get(camera=self.c)
        self.assertEqual(state.index_mode,'id')
        self.assertEqual(state.offset,4739869)
        self.assertEqual(poll(self.c),0)
        self.assertEqual(Reading.objects.count(),2)

    @patch.dict(os.environ,{'PFC_PULL_IPS':'192.168.1.235','PFC_SERVICE_SECRET':'test'})
    @patch('flow.pfc_pull.urlopen')
    def test_rejected_response_preserved_after_rollback(self,open_url):
        import json
        from django.core.management import call_command
        payload={'result':{'isError':'false','code':'0'},'rptQuantity':0}
        open_url.return_value.__enter__.return_value.read.return_value=json.dumps(payload).encode()
        call_command('poll_pfc',once=True)
        self.assertEqual(RawDelivery.objects.get().payload['response'],payload)
        self.assertEqual(RawDelivery.objects.get().payload['request']['getQuantity'],2)
        self.assertFalse(Reading.objects.exists())
        self.assertFalse(PullCursor.objects.get(camera=self.c).initialized)
