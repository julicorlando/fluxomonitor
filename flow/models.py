from django.db import models
class Camera(models.Model):
    name=models.CharField(max_length=100)
    ip=models.GenericIPAddressField(unique=True)
    serial=models.CharField(max_length=100,unique=True,null=True,blank=True)
    enabled=models.BooleanField(default=True)
    last_seen=models.DateTimeField(null=True,blank=True)
    location=models.CharField(max_length=150,blank=True)
    manufacturer=models.CharField(max_length=100,blank=True)
    model=models.CharField(max_length=100,blank=True)
    api_url=models.URLField(blank=True,help_text='Endereço documentado da API web do equipamento; não consultado automaticamente.')
    class Meta:
        verbose_name='Dispositivo'
        verbose_name_plural='Dispositivos'
    def __str__(self): return self.name
class ApiKey(models.Model):
    name=models.CharField(max_length=100)
    digest=models.CharField(max_length=64,unique=True)
    camera=models.ForeignKey(Camera,null=True,blank=True,on_delete=models.CASCADE)
    can_ingest=models.BooleanField(default=False)
    can_read=models.BooleanField(default=True)
    enabled=models.BooleanField(default=True)
    def __str__(self): return self.name
class RawDelivery(models.Model):
    camera=models.ForeignKey(Camera,on_delete=models.PROTECT)
    received_at=models.DateTimeField(auto_now_add=True)
    payload=models.JSONField()
    status=models.CharField(max_length=30,default='captured')
class Reading(models.Model):
    camera=models.ForeignKey(Camera,on_delete=models.PROTECT)
    event_id=models.CharField(max_length=128)
    timestamp=models.DateTimeField()
    entries=models.PositiveIntegerField()
    exits=models.PositiveIntegerField()
    mode=models.CharField(max_length=12,choices=[('delta','Intervalo'),('cumulative','Acumulado')])
    counter_epoch=models.CharField(max_length=80,blank=True)
    raw=models.ForeignKey(RawDelivery,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['camera','event_id'],name='unique_camera_event')]
        indexes=[models.Index(fields=['camera','timestamp'])]
class DailyReport(models.Model):
    day=models.DateField(unique=True)
    data=models.JSONField()
    updated_at=models.DateTimeField(auto_now=True)
