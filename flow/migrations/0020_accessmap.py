from django.db import migrations,models
class Migration(migrations.Migration):
    dependencies=[('flow','0015_import_cleanup_public_links')]
    operations=[migrations.CreateModel(name='AccessMap',fields=[('id',models.BigAutoField(auto_created=True,primary_key=True,serialize=False,verbose_name='ID')),('positions',models.JSONField(default=dict)),('image',models.BinaryField(blank=True,null=True)),('image_type',models.CharField(blank=True,max_length=30)),('updated_at',models.DateTimeField(auto_now=True))])]
