from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('helpdesk', '0005_verified_chat_identity')]
    operations = [
        migrations.CreateModel(
            name='UnresolvedInbound',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('external_id', models.CharField(max_length=250, unique=True)),
                ('sender', models.CharField(db_index=True, max_length=100)),
                ('text', models.TextField(blank=True, max_length=4000)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('last_seen_at', models.DateTimeField(auto_now=True)),
                ('attempts', models.PositiveIntegerField(default=1)),
                ('status', models.CharField(default='unresolved', max_length=24)),
            ],
        ),
    ]
