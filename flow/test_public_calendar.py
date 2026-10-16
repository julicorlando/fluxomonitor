import hashlib
from datetime import date,datetime,time,timedelta
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from .models import Camera,RawDelivery,Reading,PublicLink,HistoryImport
from .services import ZONE

class PublicCalendarTests(TestCase):
    def setUp(self):
        self.now=datetime(2026,10,16,8,tzinfo=ZONE)
        self.camera=Camera.objects.create(name='Entrada',ip='192.168.1.235')
        self.other=Camera.objects.create(name='Privado',ip='192.168.1.236')
        self.link=PublicLink.objects.create(label='Consulta',camera=self.camera,digest=hashlib.sha256(b'calendar-token').hexdigest(),start=date(2025,1,1),end=date(2025,1,2),expires_at=self.now+timedelta(days=1),actor='admin')
    def reading(self,day,clock,n,camera=None):
        c=camera or self.camera;r=RawDelivery.objects.create(camera=c,payload={})
        Reading.objects.create(camera=c,raw=r,event_id=str(r.pk),timestamp=datetime.combine(day,clock,ZONE),entries=n,exits=999,mode='delta')
    def get(self,params=None,path='/public/calendar-token/'):
        with patch('django.utils.timezone.now',return_value=self.now):return self.client.get(path,params or {})
    def test_default_month_calendar_scoped_entry_only_daily_cutoff_and_detail(self):
        day=date(2026,10,1)
        self.reading(day,time(22,10),7);self.reading(day,time(22,10,1),100)
        self.reading(day,time(10),1000,self.other)
        self.reading(self.now.date(),time(7),2);self.reading(self.now.date(),time(9),500)
        page=self.get()
        self.assertContains(page,'Calendário mensal de entradas')
        self.assertNotContains(page,'Saídas');self.assertNotContains(page,'192.168')
        self.assertNotContains(page,'Menu principal');self.assertNotContains(page,'Privado')
        self.assertEqual(page.context['data']['totals'],{'entries':9})
        cells=[c for week in page.context['month']['weeks'] for c in week if c]
        self.assertEqual(len(cells),31)
        self.assertEqual(cells[0]['entries'],7)
        self.assertFalse(cells[1]['has_data'])
        self.assertTrue(cells[15]['partial']);self.assertTrue(cells[16]['future'])
        detail=self.get({'view':'reports','mode':'day','reference':str(day)})
        self.assertEqual(detail.context['data']['totals']['entries'],7)
        self.assertContains(detail,'Calendário de entradas')
    def test_month_navigation_any_history_pending_and_expiry(self):
        old=date(2024,2,2);self.reading(old,time(10),4)
        HistoryImport.objects.create(camera=self.camera,start=old,end=old,status='paused')
        page=self.get({'view':'calendar','reference':str(old)})
        self.assertEqual(page.context['data']['totals']['entries'],4)
        cells=[c for w in page.context['month']['weeks'] for c in w if c]
        self.assertEqual(len(cells),29);self.assertTrue(cells[1]['pending'])
        self.assertEqual(page.context['month']['previous'],date(2024,1,1))
        self.assertEqual(page.context['month']['following'],date(2024,3,1))
        self.assertContains(page,'Imprimir / salvar PDF')
        self.link.expires_at=self.now-timedelta(seconds=1);self.link.save()
        self.assertEqual(self.get().status_code,404)
    def test_preview_requires_superadmin_does_not_create_or_count_links(self):
        path='/public-links/preview/'
        self.assertEqual(self.get(path=path).status_code,302)
        u=User.objects.create_user('staff',is_staff=True);self.client.force_login(u)
        self.assertEqual(self.get(path=path).status_code,302)
        u.is_superuser=True;u.save()
        page=self.get(path=path)
        self.assertContains(page,'Calendário mensal de entradas')
        self.assertContains(page,'Prévia exclusiva do superadmin')
        self.assertEqual(PublicLink.objects.count(),1)
        self.link.refresh_from_db();self.assertEqual(self.link.access_count,0)
        self.assertEqual(page['Referrer-Policy'],'no-referrer')
