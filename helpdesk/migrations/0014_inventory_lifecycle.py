from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [('helpdesk','0013_asset_documents')]
    operations = [
        migrations.AddField(model_name='asset',name='product_code',field=models.CharField(blank=True,max_length=120)),
        migrations.AddField(model_name='asset',name='warranty_expires_at',field=models.DateField(blank=True,null=True)),
        migrations.AddField(model_name='asset',name='purchase_value',field=models.DecimalField(blank=True,decimal_places=2,max_digits=12,null=True)),
        migrations.AddField(model_name='asset',name='tech_specs',field=models.JSONField(blank=True,default=dict)),
        migrations.AddField(model_name='asset',name='disposed_at',field=models.DateField(blank=True,null=True)),
        migrations.AddField(model_name='asset',name='disposal_reason',field=models.TextField(blank=True)),
        migrations.CreateModel(name='AssetMovement',fields=[
            ('id',models.BigAutoField(primary_key=True,serialize=False,auto_created=True,verbose_name='ID')),
            ('action',models.CharField(choices=[('handover','Entrega'),('return','Devolução'),('transfer','Transferência'),('loan','Empréstimo')],max_length=12)),
            ('old_location',models.CharField(blank=True,max_length=200)),('new_location',models.CharField(blank=True,max_length=200)),
            ('reason',models.TextField(max_length=2000)),('author',models.CharField(max_length=150)),
            ('created_at',models.DateTimeField(auto_now_add=True)),
            ('asset',models.ForeignKey(to='helpdesk.asset',related_name='movements',on_delete=django.db.models.deletion.PROTECT)),
            ('old_company',models.ForeignKey(to='helpdesk.company',related_name='+',on_delete=django.db.models.deletion.PROTECT)),
            ('new_company',models.ForeignKey(to='helpdesk.company',related_name='+',on_delete=django.db.models.deletion.PROTECT)),
            ('old_contact',models.ForeignKey(to='helpdesk.contact',related_name='+',null=True,blank=True,on_delete=django.db.models.deletion.PROTECT)),
            ('new_contact',models.ForeignKey(to='helpdesk.contact',related_name='+',null=True,blank=True,on_delete=django.db.models.deletion.PROTECT)),
        ]),
        migrations.CreateModel(name='InventoryCheck',fields=[
            ('id',models.BigAutoField(primary_key=True,serialize=False,auto_created=True,verbose_name='ID')),
            ('found',models.BooleanField(default=True)),('location',models.CharField(blank=True,max_length=200)),
            ('notes',models.TextField(blank=True,max_length=1000)),('checked_by',models.CharField(max_length=150)),
            ('checked_at',models.DateTimeField(auto_now_add=True)),
            ('asset',models.ForeignKey(to='helpdesk.asset',related_name='checks',on_delete=django.db.models.deletion.PROTECT)),
        ]),
        migrations.CreateModel(name='AssetDisposal',fields=[
            ('id',models.BigAutoField(primary_key=True,serialize=False,auto_created=True,verbose_name='ID')),
            ('reason',models.TextField(max_length=2000)),('data_sanitized',models.BooleanField(default=False)),
            ('approved_by',models.CharField(max_length=150)),('disposed_on',models.DateField()),
            ('created_at',models.DateTimeField(auto_now_add=True)),
            ('asset',models.OneToOneField(to='helpdesk.asset',related_name='disposal',on_delete=django.db.models.deletion.PROTECT)),
        ]),
    ]
