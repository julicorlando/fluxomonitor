from decimal import Decimal
from datetime import datetime,time
from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from .adjustments import adjusted_entries
from .models import WhatsAppConfig,WhatsAppDelivery,AuditLog
from .services import ZONE
from .whatsapp import scheduled_send,send_test
from .whatsapp_views import ConfigForm

class MarginTests(TestCase):
    def test_exact_discount_once_rounding_and_boundaries(self):
        self.assertEqual(adjusted_entries(1000,'5')['adjusted'],950)
        self.assertEqual(adjusted_entries(3,'10')['adjusted'],2)
        self.assertEqual(adjusted_entries(3,'0')['adjusted'],3)
        self.assertEqual(adjusted_entries(3,'100')['adjusted'],0)
        self.assertEqual(adjusted_entries(0,'12.25')['adjusted'],0)
        self.assertEqual(adjusted_entries(10000,'0.01')['adjusted'],9999)
        for value in ('-1','100.01','NaN','Infinity'):
            with self.assertRaises(ValueError):adjusted_entries(100,value)
    def test_form_validates_percentage(self):
        for percent in ('-1','100.01','1.234','NaN',''):
            f=ConfigForm({'enabled':'','error_margin_percent':percent,'send_count':1,'times':'22:10'},instance=WhatsAppConfig())
            self.assertFalse(f.is_valid());self.assertIn('error_margin_percent',f.errors)
        f=ConfigForm({'error_margin_percent':'5.25','send_count':1,'times':'22:10'},instance=WhatsAppConfig())
        self.assertTrue(f.is_valid(),f.errors)
    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('flow.whatsapp.gateway',return_value={'id':'ok'})
    @patch('flow.whatsapp.verified_total',return_value=101)
    def test_schedule_and_test_use_verified_original_and_audit_snapshot(self,verify,gateway,session):
        cfg=WhatsAppConfig.objects.create(pk=1,enabled=True,chat_ids=['one@c.us','two@g.us'],error_margin_percent=Decimal('5'))
        now=datetime(2026,10,16,22,10,tzinfo=ZONE)
        scheduled_send(now);scheduled_send(now)
        self.assertEqual(gateway.call_count,2);self.assertEqual(verify.call_count,1)
        for d in WhatsAppDelivery.objects.all():
            self.assertEqual(d.adjustment,{'original':101,'adjusted':95,'deducted':6,'percent':'5.00'})
            self.assertIn('fluxo final 95',d.text)
        cfg.error_margin_percent=10;cfg.save()
        send_test(cfg,'test')
        self.assertEqual(AuditLog.objects.filter(action='whatsapp_test').last().changes['adjustment']['adjusted'],90)
        self.assertEqual(WhatsAppDelivery.objects.first().adjustment['percent'],'5.00')
    def test_admin_view_and_api_leave_original_totals_and_viewer_unchanged(self):
        WhatsAppConfig.objects.create(pk=1,error_margin_percent=5)
        admin=User.objects.create_superuser('admin','a@example.com','test')
        self.client.force_login(admin)
        self.assertContains(self.client.get('/'),'Com e sem margem de erro')
        data=self.client.get('/api/v1/live/').json()
        self.assertEqual(data['adjustment']['original'],data['totals']['entries'])
        self.assertEqual(data['adjustment']['percent'],'5.00')
        data=self.client.get('/api/v1/insights/').json()
        self.assertIn('adjustment',data['current'])
        self.client.force_login(User.objects.create_user('viewer'))
        self.assertNotContains(self.client.get('/'),'Com e sem margem de erro')
        self.assertNotIn('adjustment',self.client.get('/api/v1/live/').json())
        self.assertNotIn('adjustment',self.client.get('/api/v1/insights/').json()['current'])

    @patch('flow.whatsapp_views.session_info',return_value={'status':'STOPPED'})
    def test_setting_save_preview_invalid_and_old_form(self,session):
        admin=User.objects.create_superuser('owner','owner@example.com','test');self.client.force_login(admin)
        cfg=WhatsAppConfig.objects.create(pk=1,error_margin_percent=5)
        self.assertContains(self.client.get('/whatsapp/'),'Margem de erro (%)')
        data={'error_margin_percent':'12.50','send_count':1,'times':'22:10'}
        self.assertEqual(self.client.post('/whatsapp/',data).status_code,302)
        cfg.refresh_from_db();self.assertEqual(cfg.error_margin_percent,Decimal('12.50'))
        self.assertEqual(AuditLog.objects.get(action='margin_changed').changes['before'],'5.00')
        data['error_margin_percent']='150'
        self.assertEqual(self.client.post('/whatsapp/',data).status_code,200)
        cfg.refresh_from_db();self.assertEqual(cfg.error_margin_percent,Decimal('12.50'))
        del data['error_margin_percent']
        self.assertEqual(self.client.post('/whatsapp/',data).status_code,302)
        cfg.refresh_from_db();self.assertEqual(cfg.error_margin_percent,Decimal('12.50'))
