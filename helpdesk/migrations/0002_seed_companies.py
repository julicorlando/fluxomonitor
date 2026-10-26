from django.db import migrations


def seed(apps, schema_editor):
    Company = apps.get_model('helpdesk', 'Company')
    for code in ('PDU', 'CM', 'SC', 'EVPI', 'PRED', 'FRT', 'TSP'):
        Company.objects.get_or_create(code=code, defaults={'name': code})
    apps.get_model('helpdesk', 'BotConfig').objects.get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [('helpdesk', '0001_initial')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
