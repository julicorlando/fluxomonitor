from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [('helpdesk', '0012_seed_company_sla')]
    operations = [
        migrations.AddField(model_name='asset', name='invoice_number', field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name='asset', name='acquisition_date', field=models.DateField(blank=True, null=True)),
        migrations.CreateModel(name='AssetAttachment', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('kind', models.CharField(choices=[('photo','Fotografia'),('signed_term','Termo assinado'),('document','Documento PDF')], max_length=16)),
            ('file', models.FileField(upload_to='private_inventory/%Y/%m/')),
            ('original_name', models.CharField(max_length=180)),
            ('size_bytes', models.PositiveIntegerField()),
            ('uploaded_by', models.CharField(max_length=150)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('asset', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='attachments', to='helpdesk.asset')),
        ], options={'ordering':['-created_at','-id']}),
    ]
