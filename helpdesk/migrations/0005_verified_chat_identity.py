from django.db import migrations, models
import django.db.models.deletion

class Migration(migrations.Migration):
    dependencies = [('helpdesk', '0004_inbound_support_inbox_pending_and_more')]
    operations = [
        migrations.CreateModel(
            name='VerifiedChatIdentity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('phone', models.CharField(max_length=14)),
                ('chat_id', models.CharField(db_index=True, max_length=80)),
                ('checked_at', models.DateTimeField(auto_now=True)),
                ('contact', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='verified_chat', to='helpdesk.contact')),
            ],
            options={'indexes': [models.Index(fields=['chat_id', 'phone'], name='support_chat_identity')]},
        ),
    ]
