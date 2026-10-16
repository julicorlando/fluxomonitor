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
        self.assertEqual(self.client.get('/public/testtoken/data/',{'mode':'year','reference':'2023-01-01'}).status_code,200)
    def add_event(self,day,at,entries=1):
        raw=RawDelivery.objects.create(camera=self.camera,payload={})
        return Reading.objects.create(camera=self.camera,event_id=f'cutoff-{raw.pk}',timestamp=datetime.combine(day,at,tzinfo=ZONE),entries=entries,exits=12345,mode='delta',raw=raw)

    def test_daily_cutoff_in_all_periods_and_entry_only_payload(self):
        self.add_event(self.day,time(22,9,59),2)
        self.add_event(self.day,time(22,10),4)
        self.add_event(self.day,time(22,10,1),1000)
        self.add_event(self.day,time(23,59),1000)
        for mode in ('day','month','year','range'):
            query={'mode':mode,'reference':str(self.day),'end':str(self.day)}
            response=self.client.get('/public/testtoken/data/',query)
            self.assertEqual(response.status_code,200)
            data=response.json()['data']
            self.assertEqual(data['totals'],{'entries':9})
            self.assertTrue(all(set(p)=={'label','entries'} for p in data['points']))
            self.assertNotIn('exits',str(data));self.assertNotIn('passages',str(data))
        page=self.client.get('/public/testtoken/',{'mode':'day','reference':str(self.day)})
        self.assertNotContains(page,'Saídas');self.assertNotContains(page,'Saldo calculado')
        self.assertContains(page,'22:10')

    def test_each_local_day_is_cut_off_and_legacy_dates_do_not_limit_history(self):
        self.link.start=self.day;self.link.end=self.day;self.link.save()
        yesterday=self.day-timedelta(days=1)
        self.add_event(yesterday,time(22,10),7)
        self.add_event(yesterday,time(22,11),1000)
        self.add_event(self.day,time(0,1),2)
        data=analytics(self.link,'range',yesterday,self.day)
        self.assertEqual(data['totals']['entries'],12)
        self.assertEqual(len(data['points']),2)
        self.assertEqual(analytics(self.link,'day',yesterday)['totals']['entries'],7)
        # Existing links also see new days, beyond their old end date.
        later=self.day+timedelta(days=1)
        self.add_event(later,time(10),5)
        self.assertEqual(analytics(self.link,'day',later)['totals']['entries'],5)

    def test_current_day_is_partial_and_future_events_are_excluded(self):
        now=timezone.now().astimezone(ZONE).replace(hour=12,minute=0,second=0,microsecond=0)
        self.link.expires_at=now+timedelta(days=1);self.link.save()
        self.add_event(now.date(),time(11),6)
        self.add_event(now.date(),time(13),1000)
        with patch('flow.public_links.timezone.now',return_value=now):
            data=analytics(self.link,'day',now.date())
            self.assertEqual(data['totals']['entries'],6);self.assertTrue(data['partial'])
            response=self.client.get('/public/testtoken/data/')
            self.assertEqual(response.json()['data']['start'],str(now.date()))
        with patch('flow.public_links.timezone.now',return_value=now.replace(hour=23)):
            self.assertFalse(analytics(self.link,'day',now.date())['partial'])

    def test_create_only_requires_label_and_duration(self):
        self.client.force_login(self.user)
        response=self.client.post('/public-links/',{'label':'Livre','duration_days':2})
        self.assertContains(response,'Link público gerado')
        link=PublicLink.objects.latest('pk')
        self.assertIsNone(link.camera_id)
        self.assertEqual(link.start,date(2000,1,1))
        self.assertNotContains(response,'name="start"');self.assertNotContains(response,'name="end"')
        self.assertNotContains(response,'name="camera"')

    def test_custom_range_comparison_and_invalid_dates(self):
        query={'mode':'range','reference':'2025-05-01','end':'2025-05-31','compare':'2024-05-01','compare_end':'2024-05-31'}
        response=self.client.get('/public/testtoken/data/',query)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['data']['totals']['entries'],3)
        self.assertEqual(response.json()['comparison']['totals']['entries'],0)
        for bad in ({'mode':'range','reference':str(self.day)}, {'mode':'range','reference':str(self.day),'end':'2024-01-01'}, {'mode':'day','reference':'1999-01-01'}, {'mode':'day','reference':'9999-01-01'}):
            self.assertEqual(self.client.get('/public/testtoken/data/',bad).status_code,400)

    def test_public_page_access_counts_reloads_but_not_json_head_or_post(self):
        self.client.get('/public/testtoken/')
        self.client.get('/public/testtoken/',{'mode':'day','reference':str(self.day)})
        self.link.refresh_from_db()
        self.assertEqual(self.link.access_count,2)
        self.assertIsNotNone(self.link.last_access_at)
        first=self.link.last_access_at
        self.client.get('/public/testtoken/data/')
        self.client.head('/public/testtoken/')
        self.client.post('/public/testtoken/',{})
        self.link.refresh_from_db()
        self.assertEqual(self.link.access_count,2);self.assertEqual(self.link.last_access_at,first)
        payload=self.client.get('/public/testtoken/data/').json()
        self.assertNotIn('access_count',str(payload))
        self.client.force_login(self.user)
        page=self.client.get('/public-links/')
        self.assertContains(page,'2 acessos');self.assertContains(page,'Último acesso:')
        self.assertContains(page,'Não representam pessoas únicas')
        self.link.refresh_from_db();self.assertEqual(self.link.access_count,2)

    def test_unavailable_public_links_do_not_count_access(self):
        self.link.expires_at=timezone.now()-timedelta(seconds=1);self.link.save()
        self.client.get('/public/testtoken/')
        self.link.refresh_from_db();self.assertEqual(self.link.access_count,0)
        self.link.expires_at=timezone.now()+timedelta(days=1);self.link.revoked_at=timezone.now();self.link.save()
        self.client.get('/public/testtoken/')
        self.client.get('/public/invalid/')
        self.link.refresh_from_db();self.assertEqual(self.link.access_count,0)
        self.assertIsNone(self.link.last_access_at)

    def test_last_access_never_moves_backwards(self):
        future=timezone.now()+timedelta(seconds=1)
        self.link.last_access_at=future;self.link.access_count=10;self.link.save()
        self.client.get('/public/testtoken/')
        self.link.refresh_from_db()
        self.assertEqual(self.link.access_count,11);self.assertEqual(self.link.last_access_at,future)

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
        response=self.client.post('/public-links/',{'label':'Relatório','duration_days':7,'start':'2024-01-01','end':'2025-12-31','camera':self.camera.pk})
        self.assertContains(response,'Link público gerado')
        created=PublicLink.objects.latest('id');self.assertEqual(len(created.digest),64)
        from .models import AuditLog
        self.assertNotIn(created.digest,str(AuditLog.objects.latest('id').changes))
        self.client.post('/public-links/',{'action':'revoke','link_id':created.pk});created.refresh_from_db();self.assertIsNotNone(created.revoked_at)
    def test_https_admin_form_keeps_csrf_provenance_and_public_url_private(self):
        from django.test import Client,override_settings
        with override_settings(ALLOWED_HOSTS=['fluxo.axionwebdigital.com.br']):
            client=Client(enforce_csrf_checks=True);client.force_login(self.user)
            host={'HTTP_HOST':'fluxo.axionwebdigital.com.br'}
            response=client.get('/public-links/',secure=True,**host)
            self.assertEqual(response['Referrer-Policy'],'same-origin')
            token=client.cookies['csrftoken'].value
            body={'csrfmiddlewaretoken':token,'label':'Consulta segura','duration_days':2,'start':'2025-05-10','end':'2025-05-10'}
            self.assertEqual(client.post('/public-links/',body,secure=True,HTTP_ORIGIN='https://evil.example',**host).status_code,403)
            self.assertEqual(client.post('/public-links/',body,secure=True,**host).status_code,403)
            response=client.post('/public-links/',body,secure=True,HTTP_REFERER='https://fluxo.axionwebdigital.com.br/public-links/',**host)
            self.assertContains(response,'Link público gerado')
            self.assertEqual(client.get('/public/testtoken/',secure=True,**host)['Referrer-Policy'],'no-referrer')

    def test_extend_same_link_expired_allowed_revoked_blocked(self):
        self.client.force_login(self.user)
        expiry=self.link.expires_at;digest=self.link.digest
        self.client.post('/public-links/',{'action':'extend','link_id':self.link.pk,'days':3})
        self.link.refresh_from_db();self.assertEqual(self.link.expires_at,expiry+timedelta(days=3));self.assertEqual(self.link.digest,digest)
        self.link.expires_at=timezone.now()-timedelta(days=2);self.link.save()
        self.client.post('/public-links/',{'action':'extend','link_id':self.link.pk,'days':1})
        self.link.refresh_from_db();self.assertGreater(self.link.expires_at,timezone.now())
        self.link.revoked_at=timezone.now();self.link.save();expiry=self.link.expires_at
        response=self.client.post('/public-links/',{'action':'extend','link_id':self.link.pk,'days':1})
        self.link.refresh_from_db();self.assertEqual(self.link.expires_at,expiry);self.assertContains(response,'não esteja revogado')

    def test_cumulative_is_not_silently_summed(self):
        Reading.objects.filter(camera=self.camera).update(mode='cumulative')
        with self.assertRaisesMessage(ValueError,'acumulados'):analytics(self.link,'year',self.day)

class TodayReimportTests(TestCase):
    @patch.dict('os.environ',{'PFC_PULL_IPS':'192.168.1.235,192.168.1.237'})
    def test_today_only_is_backed_up_and_one_job_per_physical_device(self):
        from .import_cleanup import reimport_today
        day=timezone.now().astimezone(ZONE).date()
        camera=Camera.objects.create(ip='192.168.1.235',name='A')
        source=Camera.objects.create(ip='192.168.1.237',name='B')
        target=Camera.objects.create(ip='192.168.1.237',name='Replica')
        raw=RawDelivery.objects.create(camera=camera,payload={'source':'pfc-pull'})
        today=Reading.objects.create(camera=camera,event_id='pfc-event:1',timestamp=timezone.now()-timedelta(seconds=5),entries=1,exits=0,mode='delta',raw=raw)
        yesterday=Reading.objects.create(camera=camera,event_id='pfc-event:2',timestamp=timezone.now()-timedelta(days=1),entries=1,exits=0,mode='delta',raw=raw)
        PullCursor.objects.create(camera=camera,offset=100)
        archive,jobs=reimport_today('test')
        from .models import AuditLog
        for audit in AuditLog.objects.all():AuditLog._meta.get_field('action').clean(audit.action,audit)
        self.assertEqual(len(jobs),2);self.assertEqual({j.camera_id for j in jobs},{camera.pk,source.pk})
        self.assertTrue(all(j.start==day and j.end==day for j in jobs))
        self.assertFalse(Reading.objects.filter(pk=today.pk).exists());self.assertTrue(Reading.objects.filter(pk=yesterday.pk).exists())
        self.assertEqual(PullCursor.objects.get(camera=camera).offset,100)
        restore(archive.pk,'test');self.assertTrue(Reading.objects.filter(pk=today.pk).exists())
