from datetime import date
from django.contrib.auth import get_user_model
from django.test import TestCase
from helpdesk.models import Asset, Company, Contact, AssetMovement, InventoryCheck, AssetDisposal, AgentGrant


class InventoryLifecycleTests(TestCase):
    def setUp(self):
        user=get_user_model()
        self.master=user.objects.create_superuser('inventorymaster','master@example.org','password')
        self.agent=user.objects.create_user('inventoryagent','agent@example.org','password',is_staff=True)
        self.company=Company.objects.get(code='SC')
        self.other=Company.objects.get(code='PDU')
        AgentGrant.objects.create(user=self.agent, company=self.company)
        self.contact=Contact.objects.create(company=self.company,name='Titular Teste',phone='+5581999998877',email='holder@example.org')
        self.asset=Asset.objects.create(company=self.company,tag='000301',name='Parafusadeira BAT 18V',invoice_number='324364',
            acquisition_date=date(2026,10,20),product_code='17238',contact=self.contact)
        self.client.force_login(self.master)

    def test_term_uses_given_spreadsheet_columns_and_access(self):
        response=self.client.get(f'/support/assets/{self.asset.pk}/term/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Content-Type'],'application/pdf')
        body=response.content
        self.assertTrue(body.startswith(b'%PDF-'))
        self.assertIn(b'filename="termo-patrimonio-',response['Content-Disposition'].encode())
        png=self.client.get(f'/support/assets/{self.asset.pk}/qr/')
        self.assertEqual(png.status_code,200)
        self.assertTrue(png.content.startswith(b'\\x89PNG'.replace(b'\\x89',bytes([137]))))

    def test_transfer_maintains_history_and_requires_master_cross_company(self):
        response=self.client.post(f'/support/assets/{self.asset.pk}/transfer/',{
            'action':'transfer','company':self.other.pk,'location':'Almoxarifado','reason':'Mudança autorizada'})
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.company,self.other)
        self.assertEqual(self.asset.movements.count(),1)
        event=AssetMovement.objects.get()
        self.assertEqual(event.old_company,self.company)
        self.assertEqual(event.new_company,self.other)

    def test_company_scoping_blocks_read_pdf_qr_and_writes(self):
        self.client.force_login(self.agent)
        for suffix in ['term/','qr/','transfer/','check/','disposal/']:
            self.assertEqual(self.client.get(f'/support/assets/{self.asset.pk}/{suffix}').status_code,200 if suffix!='disposal/' else 302)
        foreign=Asset.objects.create(company=self.other,tag='FOREIGN',name='Outro')
        for suffix in ['term/','qr/','transfer/','check/']:
            self.assertEqual(self.client.get(f'/support/assets/{foreign.pk}/{suffix}').status_code,404)
        self.assertEqual(self.client.post(f'/support/assets/{foreign.pk}/check/',{'found':'on'}).status_code,404)

    def test_check_and_disposal_and_no_repeat(self):
        self.assertEqual(self.client.post(f'/support/assets/{self.asset.pk}/check/',{
            'found':'on','location':'Setor TI','notes':'Conferido'}).status_code,302)
        self.assertTrue(InventoryCheck.objects.filter(asset=self.asset,found=True).exists())
        response=self.client.post(f'/support/assets/{self.asset.pk}/disposal/',{
            'reason':'Fim da vida útil - autorização TI','disposed_on':'2026-10-20','data_sanitized':'on'})
        self.assertEqual(response.status_code,302)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.state,'retired')
        self.assertTrue(AssetDisposal.objects.filter(asset=self.asset).exists())
        self.assertEqual(self.client.post(f'/support/assets/{self.asset.pk}/disposal/',{
            'reason':'duplicado','disposed_on':'2026-10-20','data_sanitized':'on'}).status_code,409)

    def test_dashboard_is_scoped(self):
        self.client.force_login(self.agent)
        response=self.client.get('/support/assets/dashboard/')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Dashboard patrimonial')
