from django.db import models
class DeviceGroup(models.Model):
    name=models.CharField(max_length=100,unique=True)
    def __str__(self):return self.name
class Camera(models.Model):
    name=models.CharField(max_length=100)
    ip=models.GenericIPAddressField(unique=True)
    serial=models.CharField(max_length=100,unique=True,null=True,blank=True)
    enabled=models.BooleanField(default=True)
    last_seen=models.DateTimeField(null=True,blank=True)
    group=models.ForeignKey(DeviceGroup,null=True,blank=True,on_delete=models.SET_NULL)
    include_in_total=models.BooleanField(default=True)
    expected_interval=models.PositiveIntegerField(default=60,help_text='Intervalo esperado em segundos')
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

class OpeningHours(models.Model):
    weekday=models.PositiveSmallIntegerField(unique=True,choices=[(0,'Segunda'),(1,'Terça'),(2,'Quarta'),(3,'Quinta'),(4,'Sexta'),(5,'Sábado'),(6,'Domingo')])
    closed=models.BooleanField(default=False)
    opens=models.TimeField(default='00:00')
    closes=models.TimeField(default='23:59:59')
class CalendarException(models.Model):
    day=models.DateField(unique=True)
    closed=models.BooleanField(default=True)
    description=models.CharField(max_length=150)
class FlowEvent(models.Model):
    title=models.CharField(max_length=150)
    day=models.DateField()
    group=models.ForeignKey(DeviceGroup,null=True,blank=True,on_delete=models.SET_NULL)
    notes=models.TextField(blank=True)
class AuditLog(models.Model):
    at=models.DateTimeField(auto_now_add=True)
    actor=models.CharField(max_length=150,default='sistema')
    action=models.CharField(max_length=20)
    object_type=models.CharField(max_length=80)
    object_id=models.CharField(max_length=80)
    changes=models.JSONField(default=dict)
class DeviceAlert(models.Model):
    camera=models.ForeignKey(Camera,on_delete=models.CASCADE)
    opened_at=models.DateTimeField()
    resolved_at=models.DateTimeField(null=True,blank=True)
    reason=models.CharField(max_length=150)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['camera'],condition=models.Q(resolved_at__isnull=True),name='one_open_alert_per_device')]
class ScheduledSummary(models.Model):
    kind=models.CharField(max_length=10)
    start=models.DateField()
    end=models.DateField()
    generated_at=models.DateTimeField(auto_now=True)
    data=models.JSONField()
    class Meta:
        constraints=[models.UniqueConstraint(fields=['kind','start','end'],name='unique_summary_period')]

class WhatsAppConfig(models.Model):
    enabled=models.BooleanField(default=False)
    chat_id=models.CharField(max_length=100,blank=True)
    recipient_label=models.CharField(max_length=150,blank=True)
    metric=models.CharField(max_length=20,default='entries',choices=[('entries','Entradas'),('passages','Entradas + saídas')])
    send_at=models.TimeField(default='22:10')
class WhatsAppDelivery(models.Model):
    day=models.DateField(unique=True)
    chat_id=models.CharField(max_length=100)
    text=models.TextField()
    status=models.CharField(max_length=30)
    detail=models.CharField(max_length=250,blank=True)
    gateway_id=models.CharField(max_length=150,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)

class NativeWindow(models.Model):
    camera=models.ForeignKey(Camera,on_delete=models.PROTECT)
    start_time=models.BigIntegerField()
    end_time=models.BigIntegerField()
    version_time=models.BigIntegerField()
    payload=models.JSONField()
    reading=models.OneToOneField(Reading,on_delete=models.PROTECT)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['camera','end_time'],name='unique_native_window')]

class PullCursor(models.Model):
    index_mode=models.CharField(max_length=10,default="offset")
    camera=models.OneToOneField(Camera,on_delete=models.CASCADE)
    offset=models.BigIntegerField(default=0)
    last_id=models.BigIntegerField(null=True,blank=True)
    initialized=models.BooleanField(default=False)
    last_success=models.DateTimeField(null=True,blank=True)
    last_error=models.CharField(max_length=250,blank=True)
