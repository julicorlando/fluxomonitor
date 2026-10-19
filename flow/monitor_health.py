"""Minimal availability only: no device IDs, counts, tokens or recipients exposed."""
from datetime import timedelta
from django.http import JsonResponse
from django.utils import timezone
from .models import MonitorSnapshot,ServicePulse

def refresh_snapshot():
    from .whatsapp import session_info
    now=timezone.now()
    try:working=session_info().get('status')=='WORKING'
    except Exception:working=False
    pulses={p.name:p.checked_at for p in ServicePulse.objects.filter(name__in=['collector','closer','history'])}
    healthy=working and all(name in pulses and now-pulses[name]<timedelta(minutes=minutes) for name,minutes in [('collector',5),('closer',10),('history',5)])
    MonitorSnapshot.objects.update_or_create(pk=1,defaults={'checked_at':now,'healthy':healthy})

def monitor_health(request):
    try:
        snap=MonitorSnapshot.objects.filter(pk=1).first()
        healthy=bool(snap and snap.healthy and timezone.now()-snap.checked_at<timedelta(minutes=3))
        body={'status':'ok' if healthy else 'degraded','observed_at':snap.checked_at.isoformat() if snap else None}
    except Exception:healthy=False;body={'status':'degraded','observed_at':None}
    response=JsonResponse(body,status=200 if healthy else 503);response['Cache-Control']='no-store';return response
