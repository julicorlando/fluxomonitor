from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from .forms import SlaPolicyForm
from .models import CompanySlaConfig, AgentGrant, Company, Contact, Conversation, Inbound, Outbound, Satisfaction, SlaPolicy, Ticket
from .reporting import dashboard
from .services import create_ticket, update_ticket
from .sla import start_sla, ticket_sla


@override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
class SlaDashboardTests(TestCase):
    def setUp(self):
        self.company = Company.objects.get(code='SC')
        self.other_company = Company.objects.get(code='CM')
        self.contact = Contact.objects.create(company=self.company, name='Colaborador teste', phone='+5581999991212', email='teste@example.com')
        self.root = get_user_model().objects.create_user('master', is_staff=True, is_superuser=True)
        self.agent = get_user_model().objects.create_user('agente', is_staff=True)
        AgentGrant.objects.create(user=self.agent, company=self.company)
        self.client.force_login(self.root)
        CompanySlaConfig.objects.filter(company=self.company).update(mode='continuous',pause_waiting=False)
        CompanySlaConfig.objects.filter(company=self.other_company).update(mode='continuous',pause_waiting=False)
        self.now = timezone.now()

    def ticket(self, *, company=None, tracked=True, status='open', priority='normal'):
        company = company or self.company
        number = Ticket.objects.count()+1
        t = Ticket.objects.create(company=company, contact=self.contact, number=number,
            code=f'{company.code}-{number:05d}', requester_name='Colaborador teste', requester_phone=self.contact.phone,
            requester_email=self.contact.email, target='Computador', description='Não liga', status=status, priority=priority)
        if tracked:
            start_sla(t, self.now)
            t.save()
        return t

    def update(self, ticket, **kwargs):
        return update_ticket(ticket.pk, self.root, status=kwargs.pop('status', ticket.status),
            text=kwargs.pop('text', 'Atualização técnica'), internal=kwargs.pop('internal', False),
            expected_revision=ticket.revision, **kwargs)

    def test_new_bot_ticket_starts_normal_sla_but_auto_reply_is_not_response(self):
        session = Conversation.objects.create(contact=self.contact, company=self.company,
            draft={'target':'Impressora', 'description':'Não imprime'})
        message = Inbound.objects.create(external_id='new-ticket', contact=self.contact, company=self.company,
            phone=self.contact.phone, text='sim', sent_at=self.now)
        ticket = create_ticket(session, message)
        self.assertEqual(ticket.priority, 'normal')
        self.assertEqual(ticket.response_due_at-ticket.sla_started_at, timedelta(hours=4))
        self.assertEqual(ticket.resolution_due_at-ticket.sla_started_at, timedelta(hours=24))
        self.assertIsNone(ticket.first_responded_at)

    def test_only_first_public_staff_update_stops_response_clock(self):
        ticket = self.update(self.ticket(), internal=True)
        self.assertIsNone(ticket.first_responded_at)
        ticket = self.update(ticket)
        first = ticket.first_responded_at
        ticket = self.update(ticket)
        self.assertEqual(ticket.first_responded_at, first)

    def test_deadlines_survive_policy_change_and_priority_uses_cycle_start(self):
        ticket = self.ticket()
        original = ticket.resolution_due_at
        SlaPolicy.objects.filter(priority='normal').update(resolution_minutes=3000)
        ticket = self.update(ticket, internal=True)
        self.assertEqual(ticket.resolution_due_at, original)
        ticket = self.update(ticket, priority='high', category='network', internal=True)
        self.assertEqual(ticket.response_due_at, ticket.sla_started_at+timedelta(hours=1))
        self.assertEqual(ticket.resolution_due_at, ticket.sla_started_at+timedelta(hours=8))
        self.assertEqual(ticket.category, 'network')
        self.assertIsNone(ticket.first_responded_at)

    def test_waiting_does_not_pause_and_reopen_starts_new_cycle(self):
        ticket = self.ticket()
        deadline = ticket.resolution_due_at
        ticket = self.update(ticket, status='waiting')
        self.assertEqual(ticket.resolution_due_at, deadline)
        ticket = self.update(ticket, status='resolved')
        resolved_at = ticket.resolved_at
        ticket = self.update(ticket, status='closed')
        self.assertEqual(ticket.resolved_at, resolved_at)
        later = self.now+timedelta(days=2)
        with patch('helpdesk.services.timezone.now', return_value=later):
            ticket = self.update(ticket, status='working')
        self.assertEqual(ticket.sla_started_at, later)
        self.assertEqual(ticket.resolution_due_at, later+timedelta(hours=24))
        self.assertIsNone(ticket.resolved_at)
        self.assertTrue(Satisfaction.objects.get(ticket=ticket).cancelled)

    def test_legacy_is_unmeasured_until_explicit_start_now(self):
        ticket = self.ticket(tracked=False)
        Ticket.objects.filter(pk=ticket.pk).update(created_at=self.now-timedelta(days=90))
        ticket = self.update(ticket, internal=True)
        self.assertIsNone(ticket.sla_started_at)
        self.assertEqual(ticket_sla(ticket)['state'], 'legacy')
        ticket = self.update(ticket, internal=True, start_sla=True)
        self.assertGreaterEqual(ticket.sla_started_at, self.now)
        self.assertIsNone(ticket.first_responded_at)

    def test_clock_boundary_risk_overdue_and_completed_measurement(self):
        ticket = self.ticket()
        self.assertEqual(ticket_sla(ticket, ticket.response_due_at)['response']['state'], 'risk')
        self.assertEqual(ticket_sla(ticket, ticket.response_due_at+timedelta(seconds=1))['response']['state'], 'breached')
        ticket.first_responded_at = ticket.response_due_at
        self.assertEqual(ticket_sla(ticket, ticket.response_due_at+timedelta(hours=2))['response']['state'], 'ok')
        ticket.resolved_at = ticket.resolution_due_at-timedelta(minutes=1)
        self.assertEqual(ticket_sla(ticket, self.now+timedelta(days=4))['state'], 'ok')

    def test_dashboard_percentages_exclude_unmeasured_and_count_open_overdue(self):
        met = self.ticket(status='resolved')
        met.first_responded_at = met.response_due_at-timedelta(minutes=1)
        met.resolved_at = met.resolution_due_at-timedelta(minutes=1)
        met.save()
        missed = self.ticket()
        missed.sla_started_at = self.now-timedelta(days=3)
        missed.response_due_at = self.now-timedelta(days=2)
        missed.resolution_due_at = self.now-timedelta(days=1)
        missed.save()
        self.ticket(tracked=False)
        self.ticket(status='cancelled')
        result = dashboard(Ticket.objects.all())
        self.assertEqual((result['response_n'], result['resolution_n']), (2,2))
        self.assertEqual((result['response_pct'], result['resolution_pct']), (50,50))
        self.assertEqual(result['overdue'], 1)
        self.assertEqual(result['legacy'], 1)
        self.assertIsNone(result['csat'])
        self.assertContains(self.client.get('/support/'), 'value="50.0" aria-label="Percentual de primeira resposta no prazo"')

    def test_satisfaction_real_ratings_and_pending_cancelled(self):
        ticket = self.ticket(status='resolved')
        for cycle, rating in enumerate([5,4,2], 1):
            Satisfaction.objects.create(ticket=ticket, cycle=cycle, rating=rating, answered_at=self.now,
                expires_at=self.now+timedelta(days=7))
        Satisfaction.objects.create(ticket=ticket, cycle=4, expires_at=self.now-timedelta(days=20))
        Satisfaction.objects.create(ticket=ticket, cycle=5, cancelled=True, expires_at=self.now)
        result = dashboard(Ticket.objects.all())
        self.assertEqual(result['csat'], 66.7)
        self.assertEqual(result['rating_avg'], 3.7)
        self.assertEqual(result['survey_pending'], 1)
        self.assertEqual(result['survey_rate'], 75)

    def test_panel_and_export_never_expose_other_company(self):
        permitted = self.ticket()
        foreign = self.ticket(company=self.other_company)
        self.client.force_login(self.agent)
        for query in ['', '?company=CM', '?format=csv', '?sla=legacy']:
            response = self.client.get('/support/'+query)
            content = b''.join(response.streaming_content).decode() if response.streaming else response.content.decode()
            self.assertNotIn(foreign.code, content)
        self.assertEqual(self.client.get(f'/support/tickets/{foreign.pk}/').status_code, 404)
        with self.assertRaises(PermissionDenied):
            update_ticket(foreign.pk, self.agent, status='working', text='Alterar SLA', internal=False,
                          expected_revision=1, priority='critical')
        self.assertEqual(self.client.get('/support/sla/').status_code, 302)
        self.assertContains(self.client.get('/support/'), permitted.code)

    def test_date_filters_errors_and_csv_formula_escaping(self):
        ticket = self.ticket()
        Ticket.objects.filter(pk=ticket.pk).update(target='=CMD(1)')
        response = self.client.get('/support/?format=csv')
        text = b''.join(response.streaming_content).decode()
        self.assertIn("'=CMD(1)", text)
        self.assertIn('no-store', response['Cache-Control'])
        response = self.client.get('/support/?start=2026-10-20&end=2026-10-01')
        self.assertContains(response, 'data inicial deve ser anterior')
        self.assertNotContains(response, ticket.code)
        self.assertEqual(self.client.get('/support/?start=invalid').status_code, 200)

    def test_dashboard_sla_filters_and_edit_form(self):
        ticket = self.ticket(priority='critical')
        self.assertContains(self.client.get('/support/?priority=critical&status=active'), ticket.code)
        self.assertNotContains(self.client.get('/support/?priority=low'), ticket.code)
        response = self.client.get(f'/support/tickets/{ticket.pk}/')
        self.assertContains(response, 'SLA do atendimento')
        self.assertContains(response, 'name="priority"')
        self.assertNotContains(response, 'name="start_sla"')
        legacy = self.ticket(tracked=False)
        self.assertContains(self.client.get(f'/support/tickets/{legacy.pk}/'), 'name="start_sla"')
        self.assertContains(self.client.get('/support/sla/'), 'Salvar políticas de SLA')

    def test_policy_form_and_post_are_csrf_protected(self):
        self.assertFalse(SlaPolicyForm({'response_minutes':60, 'resolution_minutes':30}, instance=SlaPolicy.objects.first()).is_valid())
        self.assertFalse(SlaPolicyForm({'response_minutes':0, 'resolution_minutes':30}, instance=SlaPolicy.objects.first()).is_valid())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.root)
        self.assertEqual(client.post('/support/sla/', {}).status_code, 403)
        self.assertEqual(client.post('/support/tickets/1/', {}).status_code, 403)

    def test_policy_update_preserves_existing_deadlines_and_logs(self):
        from flow.models import AuditLog
        ticket = self.ticket()
        policies = list(SlaPolicy.objects.order_by('id'))
        data = {'form-TOTAL_FORMS':4,'form-INITIAL_FORMS':4,'form-MIN_NUM_FORMS':0,'form-MAX_NUM_FORMS':1000}
        for i, policy in enumerate(policies):
            data.update({f'form-{i}-id':policy.pk,f'form-{i}-response_minutes':30,f'form-{i}-resolution_minutes':600})
        self.assertEqual(self.client.post('/support/sla/', data).status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.resolution_due_at-ticket.sla_started_at, timedelta(hours=24))
        self.assertEqual(AuditLog.objects.filter(action='support_sla_policy').count(),4)

    def test_completed_sla_priority_cannot_rewrite_history_without_reopen(self):
        ticket = self.update(self.ticket(), status='resolved')
        with self.assertRaises(ValidationError):
            self.update(ticket, priority='low', internal=True)
        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 'normal')
        ticket = self.update(ticket, priority='high', status='working')
        self.assertEqual(ticket.priority, 'high')

    def test_api_exposes_sla_and_classification_with_existing_permissions(self):
        ticket = self.ticket()
        response = self.client.get(f'/api/support/tickets/{ticket.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sla']['calendar'], '24x7')
        self.assertEqual(response.json()['priority'], 'normal')
        self.assertEqual(response.json()['evaluations'], [])

    def test_live_version_changes_with_rating_and_minutes(self):
        ticket = self.ticket(status='resolved')
        survey = Satisfaction.objects.create(ticket=ticket, cycle=1, expires_at=self.now)
        before = self.client.get('/support/pulse/').json()['version']
        survey.rating, survey.answered_at = 4, self.now
        survey.save()
        self.assertNotEqual(before, self.client.get('/support/pulse/').json()['version'])
