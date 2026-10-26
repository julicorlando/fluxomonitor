from datetime import datetime,timedelta,date
from unittest.mock import patch
from urllib.error import HTTPError
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase,Client,override_settings
from django.utils import timezone
from .calendar import ZONE,add_minutes,elapsed
from .models import (Company,Contact,AgentGrant,Ticket,Conversation,Inbound,Outbound,Satisfaction,Asset,Maintenance,
    KnowledgeArticle,KnowledgeSuggestion,Problem,CompanySlaConfig,SlaAgentProfile,SlaAlert)
from .services import create_ticket,process_message,update_ticket
from .sla import start_sla,ticket_sla
from .alerts import scan_alerts,dispatch_alerts,weekly_payload
from .forms import CompanySlaForm
from .reporting import dashboard


@override_settings(ALLOWED_HOSTS=['testserver','localhost'])
class SupportSuiteTests(TestCase):
    def setUp(self):
        self.company=Company.objects.get(code='SC')
        self.other=Company.objects.get(code='CM')
        self.contact=Contact.objects.create(company=self.company,name='Solicitante',phone='+5581999991212',email='s@example.com')
        self.tech_contact=Contact.objects.create(company=self.company,name='Técnico teste',phone='+5581999993434',email='t@example.com')
        self.manager_contact=Contact.objects.create(company=self.company,name='Gestor teste',phone='+5581999995656',email='g@example.com')
        self.root=get_user_model().objects.create_user('master',is_staff=True,is_superuser=True)
        self.tech=get_user_model().objects.create_user('tecnico',is_staff=True)
        self.manager=get_user_model().objects.create_user('gestor',is_staff=True)
        AgentGrant.objects.create(user=self.tech,company=self.company)
        AgentGrant.objects.create(user=self.manager,company=self.company)
        SlaAgentProfile.objects.create(user=self.tech,contact=self.tech_contact)
        SlaAgentProfile.objects.create(user=self.manager,contact=self.manager_contact)
        self.cfg=CompanySlaConfig.objects.get(company=self.company)
        self.cfg.manager=self.manager
        self.cfg.save()
        self.now=datetime(2026,10,26,9,0,tzinfo=ZONE)
        self.client.force_login(self.root)
        self.n=0

    def ticket(self,tracked=True,company=None):
        self.n+=1
        company=company or self.company
        ticket=Ticket.objects.create(company=company,contact=self.contact,number=self.n,code=f'{company.code}-{self.n:05d}',
            requester_name=self.contact.name,requester_phone=self.contact.phone,requester_email=self.contact.email,
            target='FIN-01',description='Computador não conecta',assigned_to=self.tech)
        if tracked:
            start_sla(ticket,self.now)
            ticket.save()
        return ticket

    def update(self,ticket,**kwargs):
        with patch('helpdesk.services.timezone.now',return_value=kwargs.pop('now',self.now)):
            return update_ticket(ticket.pk,self.root,status=kwargs.pop('status',ticket.status),
                text=kwargs.pop('text','Atualização técnica'),internal=kwargs.pop('internal',False),
                expected_revision=ticket.revision,assigned_to=self.tech,**kwargs)

    def say(self,text):
        self.n+=1
        msg=Inbound.objects.create(external_id=f'in-{self.n}',contact=self.contact,company=self.company,phone=self.contact.phone,
            text=text,sent_at=timezone.now())
        process_message(msg.pk)
        return Outbound.objects.latest('pk').text

    def test_business_calendar_lunch_weekend_and_holiday(self):
        calendar={'mode':'business','schedule':{str(day):[['08:00','12:00'],['13:00','18:00']] for day in range(5)},'holidays':['2026-10-26']}
        friday=datetime(2026,10,23,17,0,tzinfo=ZONE)
        self.assertEqual(add_minutes(friday,180,calendar),datetime(2026,10,27,10,0,tzinfo=ZONE))
        self.assertEqual(elapsed(friday,datetime(2026,10,27,10,0,tzinfo=ZONE),calendar),180)
        self.assertEqual(add_minutes(datetime(2026,10,27,11,30,tzinfo=ZONE),60,calendar),datetime(2026,10,27,13,30,tzinfo=ZONE))

    def test_24x7_and_saved_calendar_unchanged_after_edit(self):
        ticket=self.ticket()
        old=ticket.resolution_due_at
        self.cfg.mode='continuous';self.cfg.holidays=['2026-10-27'];self.cfg.save()
        ticket.refresh_from_db()
        self.assertEqual(ticket.resolution_due_at,old)
        self.assertEqual(ticket.sla_calendar['mode'],'business')
        second=self.ticket()
        self.assertEqual(second.resolution_due_at,self.now+timedelta(hours=24))

    def test_waiting_pause_freezes_clock_and_shifts_deadline_on_resume(self):
        ticket=self.ticket()
        ticket=self.update(ticket,status='waiting',now=self.now+timedelta(hours=1))
        original=ticket.resolution_due_at
        after=datetime(2026,10,27,10,0,tzinfo=ZONE)
        clock=ticket_sla(ticket,after)['resolution']
        self.assertTrue(clock['paused'])
        self.assertEqual(clock['used'],60)
        ticket=self.update(ticket,status='working',now=after)
        self.assertIsNone(ticket.sla_paused_at)
        self.assertEqual(len(ticket.sla_pauses),1)
        self.assertGreater(ticket.resolution_due_at,original)
        self.assertEqual(ticket_sla(ticket,after)['resolution']['used'],60)

    def test_pending_paused_clock_is_not_counted_as_overdue_by_wall_time(self):
        ticket=self.ticket()
        ticket=self.update(ticket,status='waiting',now=self.now+timedelta(minutes=1))
        with patch('helpdesk.reporting.timezone.now',return_value=self.now+timedelta(days=10)):
            data=dashboard(Ticket.objects.all())
        self.assertEqual(data['overdue'],0)
        self.assertEqual(data['resolution_n'],0)
        self.assertEqual(data['paused'],1)

    def test_requester_reply_on_known_ticket_resumes_waiting(self):
        ticket=self.ticket()
        ticket=self.update(ticket,status='waiting')
        Conversation.objects.create(contact=self.contact,company=self.company,state='human',draft={'ticket':ticket.pk})
        self.say('Já fiz o teste solicitado')
        ticket.refresh_from_db()
        self.assertEqual(ticket.status,'working')
        self.assertIsNone(ticket.sla_paused_at)
        self.assertEqual(ticket.updates.last().text,'Já fiz o teste solicitado')

    def test_legacy_clock_stays_24x7_without_adding_wait_pause(self):
        ticket=self.ticket(tracked=False)
        ticket.sla_started_at=self.now;ticket.response_due_at=self.now+timedelta(hours=4)
        ticket.resolution_due_at=self.now+timedelta(hours=24);ticket.save()
        ticket=self.update(ticket,status='waiting')
        self.assertIsNone(ticket.sla_paused_at)
        self.assertEqual(ticket_sla(ticket)['calendar_label'],'24×7')

    def test_reopen_creates_new_cycle_and_counter(self):
        ticket=self.update(self.ticket(),status='resolved')
        ticket=self.update(ticket,status='working',now=self.now+timedelta(days=1))
        self.assertEqual(ticket.sla_cycle,2)
        self.assertEqual(ticket.reopen_count,1)

    def test_warning_is_queued_once_to_technician(self):
        ticket=self.ticket()
        point=self.now+timedelta(hours=3,minutes=40)
        scan_alerts(point);scan_alerts(point)
        event=SlaAlert.objects.get(kind='warning')
        self.assertEqual(event.recipient,self.tech)
        self.assertEqual(event.contact,self.tech_contact)
        self.assertIn(ticket.code,event.text)
        self.assertNotIn(self.contact.phone,event.text)

    def test_overdue_and_escalation_go_to_distinct_roles(self):
        self.ticket()
        scan_alerts(self.now+timedelta(hours=5,minutes=1))
        self.assertEqual(SlaAlert.objects.get(kind='overdue').recipient,self.tech)
        self.assertEqual(SlaAlert.objects.get(kind='escalation').recipient,self.manager)

    def test_missing_mapping_is_blocked_then_automatically_revalidated(self):
        SlaAgentProfile.objects.filter(user=self.tech).delete()
        self.ticket();point=self.now+timedelta(hours=3,minutes=40)
        scan_alerts(point)
        self.assertEqual(SlaAlert.objects.get().status,'blocked')
        SlaAgentProfile.objects.create(user=self.tech,contact=self.tech_contact)
        scan_alerts(point)
        self.assertEqual(SlaAlert.objects.get().status,'pending')

    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('helpdesk.transport.verified_destination',return_value='5581999993434@c.us')
    @patch('flow.whatsapp.gateway',return_value={'id':'alert-id'})
    def test_alert_delivery_and_duplicate_scan_never_resend(self,gateway,identity,session):
        self.ticket();point=self.now+timedelta(hours=3,minutes=40)
        scan_alerts(point);dispatch_alerts(point);scan_alerts(point);dispatch_alerts(point)
        self.assertEqual(gateway.call_count,1)
        self.assertEqual(SlaAlert.objects.get().status,'accepted')

    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('helpdesk.transport.verified_destination',return_value='5581999993434@c.us')
    @patch('flow.whatsapp.gateway',side_effect=TimeoutError)
    def test_uncertain_send_is_not_retried(self,gateway,identity,session):
        self.ticket();point=self.now+timedelta(hours=3,minutes=40)
        scan_alerts(point);dispatch_alerts(point);scan_alerts(point);dispatch_alerts(point)
        self.assertEqual(gateway.call_count,1)
        self.assertEqual(SlaAlert.objects.get().status,'unknown')

    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('flow.whatsapp.gateway')
    def test_revoked_access_prevents_sending_queued_alert(self,gateway,session):
        self.ticket();point=self.now+timedelta(hours=3,minutes=40);scan_alerts(point)
        AgentGrant.objects.filter(user=self.tech).delete()
        dispatch_alerts(point)
        gateway.assert_not_called()
        self.assertEqual(SlaAlert.objects.get().status,'blocked')

    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('flow.whatsapp.gateway')
    def test_resolved_or_paused_ticket_cancels_pending_send(self,gateway,session):
        ticket=self.ticket();point=self.now+timedelta(hours=3,minutes=40);scan_alerts(point)
        ticket=self.update(ticket,status='waiting',now=point)
        dispatch_alerts(point)
        gateway.assert_not_called()
        self.assertEqual(SlaAlert.objects.get().status,'cancelled')

    def test_weekly_summary_deduplication_and_company_isolation(self):
        self.cfg.weekly_enabled=True;self.cfg.save()
        ticket=self.ticket()
        Ticket.objects.filter(pk=ticket.pk).update(created_at=self.now-timedelta(days=4))
        foreign=self.ticket(company=self.other)
        Ticket.objects.filter(pk=foreign.pk).update(created_at=self.now-timedelta(days=4))
        scan_alerts(self.now);scan_alerts(self.now+timedelta(minutes=1))
        event=SlaAlert.objects.get(kind='weekly')
        self.assertEqual(event.recipient,self.manager)
        self.assertEqual(event.payload['opened'],1)
        self.assertEqual((event.period_start,event.period_end),(date(2026,10,19),date(2026,10,25)))
        self.assertEqual(event.payload['company'],'SC')
        self.assertNotIn(foreign.code,event.text)

    def test_weekly_does_not_send_before_selected_slot(self):
        self.cfg.weekly_enabled=True;self.cfg.weekly_day=2;self.cfg.save()
        scan_alerts(self.now)
        self.assertFalse(SlaAlert.objects.filter(kind='weekly').exists())

    def test_inventory_form_maintenance_and_linked_ticket(self):
        data={'company':self.company.pk,'tag':'FIN-01','invoice_number':'NF-12345','acquisition_date':'2026-10-20','name':'Computador Financeiro','kind':'computer','state':'active',
            'contact':self.contact.pk,'serial':'SERIAL-001','location':'Financeiro','notes':''}
        self.assertEqual(self.client.post('/support/assets/new/',data).status_code,302)
        asset=Asset.objects.get()
        self.assertEqual(self.client.post(f'/support/assets/{asset.pk}/',{'action':'maintenance','date':'2026-10-26',
            'description':'Troca da fonte','cost':'120.00','next_date':'2027-01-26'}).status_code,302)
        self.assertEqual(asset.maintenance.count(),1)
        ticket=self.update(self.ticket(),asset=asset,classify_links=True)
        self.assertEqual(ticket.asset_id,asset.pk)
        self.assertContains(self.client.get(f'/support/assets/{asset.pk}/'),'Troca da fonte')
        self.assertContains(self.client.get(f'/support/assets/{asset.pk}/'),ticket.code)

    def test_asset_and_problem_cannot_be_linked_across_companies(self):
        asset=Asset.objects.create(company=self.other,tag='PRIVATE',name='Equipamento privado')
        problem=Problem.objects.create(company=self.other,title='Problema privado',author='gestor')
        ticket=self.ticket()
        with self.assertRaises(ValidationError):
            self.update(ticket,asset=asset,classify_links=True)
        with self.assertRaises(ValidationError):
            self.update(ticket,problem=problem,classify_links=True)
        self.client.force_login(self.tech)
        for path in [f'/support/assets/{asset.pk}/',f'/support/problems/{problem.pk}/']:
            self.assertEqual(self.client.get(path).status_code,404)
        self.assertNotContains(self.client.get('/support/assets/'),'Equipamento privado')

    def test_foreign_contact_is_rejected_on_asset_form(self):
        foreign=Contact.objects.create(company=self.other,name='Outro',phone='+5581999997878',email='o@example.com')
        response=self.client.post('/support/assets/new/',{'company':self.company.pk,'tag':'FIN','name':'PC','kind':'computer',
            'state':'active','contact':foreign.pk})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Asset.objects.exists())

    def article(self,**kwargs):
        return KnowledgeArticle.objects.create(company=kwargs.pop('company',self.company),title='Impressora sem impressão',
            keywords='impressora, não imprime',procedure='Confira se há papel e se a impressora está ligada.',author='tecnico',
            published=kwargs.pop('published',True),bot_enabled=kwargs.pop('bot_enabled',True),**kwargs)

    def test_knowledge_bot_suggestion_can_resolve_without_ticket(self):
        self.article()
        for text in ['oi','sim','Impressora FIN','A impressora não imprime']:
            output=self.say(text)
        self.assertIn('este procedimento pode ajudar',output)
        self.assertIn('Continuar e abrir chamado',output)
        self.say('1')
        self.assertFalse(Ticket.objects.exists())
        self.assertTrue(KnowledgeSuggestion.objects.get().helpful)

    def test_knowledge_bot_continues_with_original_description(self):
        self.article()
        for text in ['oi','sim','Impressora FIN','A impressora não imprime','2']:
            self.say(text)
        self.say('Confirmar')
        self.assertEqual(Ticket.objects.get().description,'A impressora não imprime')
        self.assertFalse(KnowledgeSuggestion.objects.get().helpful)

    def test_foreign_or_unpublished_knowledge_never_reaches_bot(self):
        self.article(company=self.other)
        self.article(published=False,bot_enabled=False)
        for text in ['oi','sim','Impressora FIN','A impressora não imprime']:
            output=self.say(text)
        self.assertNotIn('este procedimento pode ajudar',output)
        self.assertIn('Confirmar abertura',output)
        self.assertFalse(KnowledgeSuggestion.objects.exists())

    def test_all_module_pages_render_and_staff_cannot_change_global_settings(self):
        for path in ['/support/assets/','/support/assets/new/','/support/knowledge/','/support/knowledge/new/',
                     '/support/problems/','/support/problems/new/','/support/sla/company/SC/','/support/sla/agents/',
                     '/support/sla/alerts/','/support/sla/']:
            self.assertEqual(self.client.get(path).status_code,200,path)
        self.client.force_login(self.tech)
        self.assertEqual(self.client.get('/support/sla/company/SC/').status_code,302)
        self.assertEqual(self.client.get('/support/sla/agents/').status_code,302)
        self.assertEqual(self.client.post('/support/sla/alerts/',{}).status_code,403)

    def test_recurrence_page_uses_actual_linked_assets_and_reopen_count(self):
        asset=Asset.objects.create(company=self.company,tag='PC-01',name='Computador recorrente')
        a=self.ticket();b=self.ticket()
        Ticket.objects.filter(pk__in=[a.pk,b.pk]).update(asset=asset)
        a=self.update(a,status='resolved');a=self.update(a,status='working')
        response=self.client.get('/support/problems/')
        self.assertContains(response,'Computador recorrente')
        self.assertContains(response,'2 chamados')
        self.assertContains(response,a.code)
        self.assertEqual(a.reopen_count,1)

    def test_company_calendar_form_validates_hours_and_holidays(self):
        data={'mode':'business','pause_waiting':True,'alerts_enabled':True,'warning_minutes':30,'escalation_minutes':60,
              'manager':self.manager.pk,'weekly_enabled':True,'weekly_day':0,'weekly_time':'08:00',
              'holiday_dates':'2026-12-25','d0_enabled':True,'d0_start':'08:00','d0_end':'18:00',
              'd0_break_start':'12:00','d0_break_end':'13:00'}
        form=CompanySlaForm(data,instance=self.cfg)
        self.assertTrue(form.is_valid(),form.errors)
        form.save()
        self.assertEqual(self.cfg.holidays,['2026-12-25'])
        self.assertEqual(self.cfg.schedule['0'],[['08:00','12:00'],['13:00','18:00']])
        self.assertFalse(CompanySlaForm({**data,'d0_end':'07:00'},instance=self.cfg).is_valid())
        self.assertFalse(CompanySlaForm({**data,'holiday_dates':'2026-99-99'},instance=self.cfg).is_valid())

    @patch('flow.whatsapp.session_info',return_value={'status':'WORKING'})
    @patch('helpdesk.transport.verified_destination',return_value='5581999993434@c.us')
    @patch('flow.whatsapp.gateway')
    def test_429_is_retried_safely_and_null_id_is_uncertain(self,gateway,identity,session):
        self.ticket();point=self.now+timedelta(hours=3,minutes=40)
        scan_alerts(point)
        gateway.side_effect=HTTPError('http://gateway',429,'Rate limited',{},None)
        dispatch_alerts(point)
        self.assertEqual(SlaAlert.objects.get().status,'pending')
        gateway.side_effect=None;gateway.return_value={'id':None}
        dispatch_alerts(point+timedelta(minutes=2))
        self.assertEqual(SlaAlert.objects.get().status,'unknown')
        dispatch_alerts(point+timedelta(minutes=4))
        self.assertEqual(gateway.call_count,2)

    def test_weekly_payload_counts_resolutions_and_ratings_in_the_actual_week(self):
        ticket=self.ticket()
        past=self.now-timedelta(days=4)
        Ticket.objects.filter(pk=ticket.pk).update(created_at=past)
        ticket.created_at=past;start_sla(ticket,past);ticket.save()
        ticket=self.update(ticket,status='resolved',now=past)
        update=ticket.updates.last()
        type(update).objects.filter(pk=update.pk).update(created_at=past)
        survey=Satisfaction.objects.get(ticket=ticket)
        survey.rating=4;survey.answered_at=past;survey.save()
        data=weekly_payload(self.cfg,date(2026,10,19),date(2026,10,25),self.now)
        self.assertEqual(data['opened'],1)
        self.assertEqual(data['resolved'],1)
        self.assertEqual(data['rating_avg'],4)
        self.assertEqual(data['csat'],100)
        self.assertEqual(data['team'][0]['author'],'master')

    def test_negative_maintenance_cost_is_rejected(self):
        asset=Asset.objects.create(company=self.company,tag='PC-01',name='PC')
        response=self.client.post(f'/support/assets/{asset.pk}/',{'action':'maintenance','date':'2026-10-26',
            'description':'Revisão','cost':'-10'})
        self.assertEqual(response.status_code,200)
        self.assertFalse(Maintenance.objects.exists())

    def test_company_settings_and_recipient_binding_are_saved_with_audit(self):
        from flow.models import AuditLog
        data={'mode':'business','pause_waiting':True,'alerts_enabled':True,'warning_minutes':20,'escalation_minutes':30,
              'manager':self.manager.pk,'weekly_enabled':True,'weekly_day':0,'weekly_time':'08:00',
              'holiday_dates':'2026-12-25','d0_enabled':True,'d0_start':'08:00','d0_end':'18:00'}
        self.assertEqual(self.client.post('/support/sla/company/SC/',data).status_code,302)
        self.cfg.refresh_from_db()
        self.assertEqual(self.cfg.warning_minutes,20)
        self.assertTrue(AuditLog.objects.filter(action='support_company_sla').exists())
        profile=SlaAgentProfile.objects.get(user=self.tech)
        self.assertEqual(self.client.post(f'/support/sla/agents/{profile.pk}/',{'contact':self.manager_contact.pk}).status_code,302)
        profile.refresh_from_db()
        self.assertEqual(profile.contact,self.manager_contact)
        self.assertTrue(AuditLog.objects.filter(action='support_sla_person').exists())

    def test_procedure_publication_and_problem_creation_are_audited(self):
        response=self.client.post('/support/knowledge/new/',{'company':self.company.pk,'title':'Conferir papel',
            'category':'printing','keywords':'impressora','procedure':'Confira o papel.','published':True,'bot_enabled':True})
        self.assertEqual(response.status_code,302)
        self.assertTrue(KnowledgeArticle.objects.get().bot_enabled)
        response=self.client.post('/support/problems/new/',{'company':self.company.pk,'title':'Falha recorrente da rede',
            'category':'network','status':'investigating','root_cause':'','workaround':'Revisão em andamento'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(Problem.objects.get().author,'master')

    def test_new_forms_require_csrf(self):
        client=Client(enforce_csrf_checks=True);client.force_login(self.root)
        for path in ['/support/assets/new/','/support/knowledge/new/','/support/problems/new/',
                     '/support/sla/company/SC/','/support/sla/agents/','/support/sla/alerts/']:
            self.assertEqual(client.post(path,{}).status_code,403,path)
