from django.db import migrations


def seed(apps, schema_editor):
    Company = apps.get_model('helpdesk','Company')
    Config = apps.get_model('helpdesk','CompanySlaConfig')
    for company in Company.objects.using(schema_editor.connection.alias).all():
        Config.objects.using(schema_editor.connection.alias).get_or_create(company=company)
    Ticket = apps.get_model('helpdesk','Ticket')
    Update = apps.get_model('helpdesk','TicketUpdate')
    from django.db.models import Count
    rows = Update.objects.using(schema_editor.connection.alias).filter(old_status__in=('resolved','closed','cancelled'),
        new_status__in=('open','working','waiting')).values('ticket_id').annotate(n=Count('id'))
    for row in rows:
        Ticket.objects.using(schema_editor.connection.alias).filter(pk=row['ticket_id']).update(reopen_count=row['n'])


class Migration(migrations.Migration):
    dependencies = [('helpdesk','0011_ticket_reopen_count_ticket_sla_calendar_and_more')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
