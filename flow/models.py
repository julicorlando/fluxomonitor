from django.db import models
from django.core.exceptions import ValidationError
class DeviceGroup(models.Model):
    name=models.CharField(max_length=100,unique=True)
    def __str__(self):return self.name
class Camera(models.Model):
    name=models.CharField(max_length=100)
    ip=models.GenericIPAddressField()
    replica_slot=models.PositiveSmallIntegerField(default=0,editable=False)
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
    def _assign_replica_slot(self):
        others=Camera.objects.filter(ip=self.ip).exclude(pk=self.pk)
        if self.ip=='192.168.1.237':
            used=set(others.values_list('replica_slot',flat=True))
            if len(used)>=2:raise ValidationError({'ip':'O IP 192.168.1.237 permite no máximo dois cadastros.'})
            if self.replica_slot in used:self.replica_slot=next(i for i in (0,1) if i not in used)
        else:
            self.replica_slot=0
            if others.exists():raise ValidationError({'ip':'Este IP já está cadastrado. Somente 192.168.1.237 permite dois cadastros.'})
    def clean(self):
        super().clean()
        self._assign_replica_slot()
    def save(self,*args,**kwargs):
        self._assign_replica_slot()
        if kwargs.get('update_fields') is not None and 'ip' in kwargs['update_fields']:
            kwargs['update_fields']=set(kwargs['update_fields'])|{'replica_slot'}
        super().save(*args,**kwargs)
    class Meta:
        constraints=[
            models.UniqueConstraint(fields=['ip','replica_slot'],name='unique_ip_replica_slot'),
            models.CheckConstraint(condition=models.Q(replica_slot=0)|(models.Q(ip='192.168.1.237')&models.Q(replica_slot=1)),name='only_237_second_slot'),
        ]
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
    is_holiday=models.BooleanField(default=False,verbose_name="Feriado / dia especial (fluxo final às 21h)")
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
    chat_ids=models.JSONField(default=list,blank=True)
    send_times=models.JSONField(default=list,blank=True)
    enabled=models.BooleanField(default=False)
    chat_id=models.CharField(max_length=100,blank=True)
    recipient_label=models.CharField(max_length=150,blank=True)
    metric=models.CharField(max_length=20,default='entries',choices=[('entries','Entradas'),('passages','Entradas + saídas')])
    send_at=models.TimeField(default='22:10')
class WhatsAppDelivery(models.Model):
    scheduled_time=models.TimeField(default='22:10')
    day=models.DateField()
    chat_id=models.CharField(max_length=100)
    text=models.TextField()
    status=models.CharField(max_length=30)
    detail=models.CharField(max_length=250,blank=True)
    gateway_id=models.CharField(max_length=150,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['day','scheduled_time','chat_id'],name='unique_whatsapp_day_time_recipient')]

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

class HistoryImport(models.Model):
    first_offset=models.BigIntegerField(null=True,blank=True)
    target_offset=models.BigIntegerField(null=True,blank=True)
    camera=models.ForeignKey(Camera,on_delete=models.PROTECT)
    start=models.DateField()
    end=models.DateField()
    status=models.CharField(max_length=20,default='pending')
    offset=models.BigIntegerField(default=0)
    index_mode=models.CharField(max_length=10,default='id')
    initialized=models.BooleanField(default=False)
    scanned=models.BigIntegerField(default=0)
    imported=models.BigIntegerField(default=0)
    last_timestamp=models.DateTimeField(null=True,blank=True)
    earliest_available=models.DateTimeField(null=True,blank=True)
    error=models.CharField(max_length=250,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    updated_at=models.DateTimeField(auto_now=True)
    @property
    def progress(self):
        if self.status=='completed':return 100
        if self.first_offset is None or self.target_offset is None:return None
        span=self.target_offset-self.first_offset
        if span<=0:return 0
        return round(max(0,min(99.9,100*(self.offset-self.first_offset)/span)),1)

class ImportCleanup(models.Model):
    created_at=models.DateTimeField(auto_now_add=True)
    actor=models.CharField(max_length=150)
    restored_at=models.DateTimeField(null=True,blank=True)
    affected_days=models.JSONField(default=list)
    data=models.JSONField(default=dict)

class PublicLink(models.Model):
    label=models.CharField(max_length=100)
    digest=models.CharField(max_length=64,unique=True)
    camera=models.ForeignKey(Camera,null=True,blank=True,on_delete=models.PROTECT)
    start=models.DateField()
    end=models.DateField()
    expires_at=models.DateTimeField()
    revoked_at=models.DateTimeField(null=True,blank=True)
    created_at=models.DateTimeField(auto_now_add=True)
    actor=models.CharField(max_length=150)

class AccessMap(models.Model):
    positions=models.JSONField(default=dict)
    image=models.BinaryField(null=True,blank=True)
    image_type=models.CharField(max_length=30,blank=True)
    updated_at=models.DateTimeField(auto_now=True)
