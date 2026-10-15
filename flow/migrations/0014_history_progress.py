from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[('flow','0013_historyimport')]
    operations=[
        migrations.AddField(model_name='historyimport',name='first_offset',field=models.BigIntegerField(null=True,blank=True)),
        migrations.AddField(model_name='historyimport',name='target_offset',field=models.BigIntegerField(null=True,blank=True)),
    ]
