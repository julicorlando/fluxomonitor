from django import forms
from django.contrib import messages
from django.shortcuts import render,redirect
from django.utils import timezone
from .models import NotificationConfig,SystemNotice,ServicePulse

class NotificationForm(forms.ModelForm):
    recipient=forms.RegexField(regex=r'^[1-9][0-9]{7,14}$',label='Número de alertas (país e DDD)',help_text='Somente dígitos. Destino exclusivo dos avisos de falha.',max_length=15)
    class Meta:
        model=NotificationConfig
        fields=['enabled','recipient']
        labels={'enabled':'Ativar avisos automáticos de erro por WhatsApp'}

def alert_context(request):
    if not request.user.is_superuser:return {}
    cfg,_=NotificationConfig.objects.get_or_create(pk=1)
    form=NotificationForm(request.POST or None,instance=cfg)
    enabled=cfg.enabled
    if request.method=='POST' and form.is_valid():
        cfg=form.save(commit=False)
        if cfg.enabled and not enabled:cfg.activated_at=timezone.now()
        cfg.save()
        from .models import AuditLog
        AuditLog.objects.create(actor=request.user.get_username(),action='notify_settings',object_type='NotificationConfig',object_id='1',changes={'enabled':cfg.enabled,'recipient_suffix':cfg.recipient[-4:]})
        messages.success(request,'Avisos operacionais salvos. O monitor verifica as falhas e os horários a cada 30 segundos.')
        return redirect('/alerts/')
    cfg.refresh_from_db()
    return {'notification_form':form,'notification_cfg':cfg,'system_notices':SystemNotice.objects.order_by('-first_seen')[:100],'service_pulses':ServicePulse.objects.order_by('name')}
