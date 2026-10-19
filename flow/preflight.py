"""Early checks never replace the fresh verification performed at send time."""
from datetime import datetime,time,timedelta
from django.db import transaction
from django.utils import timezone
from .models import WhatsAppConfig,PreflightCheck,AuditLog
from .services import ZONE
from .whatsapp import daily_times,recipients
from .verification import verified_total,VerificationError
from .notifications import notice,resolve

def preflight(now=None):
    now=now or timezone.now();local=now.astimezone(ZONE)
    cfg=WhatsAppConfig.objects.filter(pk=1,enabled=True).first()
    if not cfg or not recipients(cfg):return
    for value in daily_times(cfg,local.date()):
        slot=time.fromisoformat(value);due=datetime.combine(local.date(),slot,ZONE)
        if not due-timedelta(minutes=15)<=local<due:continue
        with transaction.atomic():
            row,created=PreflightCheck.objects.get_or_create(day=local.date(),scheduled_time=slot)
            row=PreflightCheck.objects.select_for_update().get(pk=row.pk)
            if not created and now-row.checked_at<timedelta(minutes=5):continue
            row.status='checking';row.checked_at=now;row.save()
        key=f'preflight:{local.date()}:{value}'
        try:
            total=verified_total(local.date(),lock_timeout_ms=3000)
            status='ready';reason='Conferência antecipada concluída. Nova verificação é obrigatória no envio.'
            resolve(key,now)
        except Exception as error:
            total=None;status='blocked';reason=str(error)[:300] if isinstance(error,VerificationError) else f'Conferência antecipada não concluída ({type(error).__name__}); confira os serviços e recuperações.'
            notice(key,'Pendência antes do envio do fluxo',f'Envio de {local:%d/%m/%Y} às {value} (Brasília). Motivo: {reason}',now)
        PreflightCheck.objects.filter(pk=row.pk).update(status=status,reason=reason,original_entries=total,checked_at=timezone.now())
        AuditLog.objects.create(actor='system',action='preflight',object_type='PreflightCheck',object_id=str(row.pk),changes={'day':str(local.date()),'slot':value,'status':status,'original_entries':total,'sent':False})
