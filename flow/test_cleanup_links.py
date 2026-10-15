import hashlib
from datetime import timedelta,datetime,time,date
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from .models import Camera,RawDelivery,Reading,HistoryImport,ImportCleanup,PublicLink,PullCursor
from .import_cleanup import cleanup,restore,preview
from .services import ZONE
from .public_links import analytics

class CleanupTests(TestCase):
    def setUp(self):
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        self.now=timezone.now();self.day=self.now.astimezone(ZONE).date()
    def reading(self,event,source):
        raw=RawDelivery.objects.create(camera=self.camera,payload={'source':source})
        return Reading.objects.create(camera=self.camera,event_id=event,timestamp=self.now-timedelta(seconds=10),entries=1,exits=0,mode='delta',raw=raw)
    def test_only_imports_removed_and_restorable_without_double(self):
        historical=self.reading('pfc-event:1','pfc-history');live=self.reading('pfc-event:2','pfc-pull')
        job=HistoryImport.objects.create(camera=self.camera,start=self.day,end=self.day,status='running')
        cursor=PullCursor.objects.create(camera=self.camera,offset=100)
        batch=cleanup('test')
        self.assertEqual(preview(),{'jobs':0,'readings':0})
        self.assertTrue(Reading.objects.filter(pk=live.pk).exists());self.assertFalse(Reading.objects.filter(pk=historical.pk).exists())
        cursor.refresh_from_db();self.assertEqual(cursor.offset,100)
        self.assertEqual(batch.affected_days,{str(self.day):[self.camera.pk]})
        restore(batch.pk,'test');restore(batch.pk,'test')
        self.assertEqual(Reading.objects.count(),2)
        self.assertEqual(Reading.objects.get(pk=historical.pk).timestamp,historical.timestamp)
        job.refresh_from_db();self.assertEqual(job.status,'cancelled')
    def test_cleanup_and_restore_require_superuser(self):
        self.reading('pfc-event:1','pfc-history')
        user=User.objects.create_user('staff',is_staff=True);self.client.force_login(user)
        self.assertEqual(self.client.post('/imports/',{'action':'cleanup'}).status_code,403)
        self.assertEqual(Reading.objects.count(),1)
        user.is_superuser=True;user.save()
        self.assertEqual(self.client.post('/imports/',{'action':'cleanup'}).status_code,302)
        batch=ImportCleanup.objects.get()
        self.client.post('/imports/',{'action':'restore','archive_id':batch.pk})
        self.assertEqual(Reading.objects.count(),1)
    def test_restore_conflict_is_atomic(self):
        self.reading('pfc-event:1','pfc-history');batch=cleanup('test')
        live=self.reading('pfc-event:1','pfc-pull');live.entries=2;live.save()
        with self.assertRaisesMessage(ValueError,'diverge'):restore(batch.pk,'test')
        batch.refresh_from_db();self.assertIsNone(batch.restored_at)

class PublicLinkTests(TestCase):
    def setUp(self):
        self.user=User.objects.create_user('admin',is_superuser=True,is_staff=True)
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        self.other=Camera.objects.create(name='Outra',ip='192.168.1.236')
        self.day=date(2025,5,10)
        for c,n in [(self.camera,3),(self.other,100)]:
            raw=RawDelivery.objects.create(camera=c,payload={})
            Reading.objects.create(camera=c,event_id='a',timestamp=datetime.combine(self.day,time(12),tzinfo=ZONE),entries=n,exits=1,mode='delta',raw=raw)
        self.link=PublicLink.objects.create(label='Consulta',digest=hashlib.sha256(b'testtoken').hexdigest(),camera=self.camera,start=date(2024,1,1),end=date(2025,12,31),expires_at=timezone.now()+timedelta(minutes=5),actor='admin')
    def test_anonymous_day_month_year_scope_and_privacy(self):
        for mode in ('day','month','year'):
            response=self.client.get('/public/testtoken/data/',{'mode':mode,'reference':str(self.day),'compare':'2024-05-10'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['data']['totals']['entries'],3)
            self.assertEqual(response.json()['comparison']['totals']['entries'],0)
            self.assertNotIn('192.168',response.content.decode())
            self.assertEqual(response['Cache-Control'],'private, no-store, max-age=0')
        page=self.client.get('/public/testtoken/',{'mode':'month','reference':str(self.day)})
        self.assertContains(page,'Imprimir / salvar PDF');self.assertNotContains(page,'Menu principal')
        self.assertContains(page,'value="2025-05-10"')
        self.assertEqual(self.client.get('/public/testtoken/data/',{'mode':'year','reference':'2023-01-01'}).status_code,400)
    def test_expiry_revocation_and_invalid_link(self):
        self.link.expires_at=timezone.now()-timedelta(seconds=1);self.link.save()
        self.assertEqual(self.client.get('/public/testtoken/').status_code,404)
        self.link.expires_at=timezone.now()+timedelta(minutes=5);self.link.revoked_at=timezone.now();self.link.save()
        self.assertEqual(self.client.get('/public/testtoken/data/').status_code,404)
        self.assertEqual(self.client.get('/public/invalid/').status_code,404)
    def test_only_superadmin_creates_and_revokes_no_secret_in_audit(self):
        self.assertEqual(self.client.get('/public-links/').status_code,302)
        ordinary=User.objects.create_user('ordinary',is_staff=True);self.client.force_login(ordinary)
        self.assertEqual(self.client.post('/public-links/',{}).status_code,302)
        self.client.force_login(self.user)
        response=self.client.post('/public-links/',{'label':'Relatório','duration':60,'start':'2024-01-01','end':'2025-12-31','camera':self.camera.pk})
        self.assertContains(response,'Link público gerado')
        created=PublicLink.objects.latest('id');self.assertEqual(len(created.digest),64)
        from .models import AuditLog
        self.assertNotIn(created.digest,str(AuditLog.objects.latest('id').changes))
        self.client.post('/public-links/',{'action':'revoke','link_id':created.pk});created.refresh_from_db();self.assertIsNotNone(created.revoked_at)
    def test_cumulative_is_not_silently_summed(self):
        Reading.objects.filter(camera=self.camera).update(mode='cumulative')
        with self.assertRaisesMessage(ValueError,'acumulados'):analytics(self.link,'year',self.day)
