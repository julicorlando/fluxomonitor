from django.db import migrations


def seed(apps, schema_editor):
    Policy = apps.get_model('helpdesk', 'SlaPolicy')
    for priority, response, resolution in [('critical',15,240), ('high',60,480), ('normal',240,1440), ('low',480,2880)]:
        Policy.objects.using(schema_editor.connection.alias).get_or_create(priority=priority,
            defaults={'response_minutes': response, 'resolution_minutes': resolution})


class Migration(migrations.Migration):
    dependencies = [('helpdesk', '0009_ticket_category_ticket_first_responded_at_and_more')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
