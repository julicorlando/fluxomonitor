from datetime import datetime,time,timedelta
from zoneinfo import ZoneInfo
from django.db import transaction
from django.db.models import Min
from django.utils import timezone
from .models import Camera,Reading,DailyReport
ZONE=ZoneInfo('America/Sao_Paulo')
def bounds(day):
    return datetime.combine(day,time.min,ZONE),datetime.combine(day+timedelta(days=1),time.min,ZONE)
def report(day,device_id=None):
    start,end=bounds(day); rows=[]; bins={}; warnings=[]
    for c in Camera.objects.filter(enabled=True).filter(**({"id":device_id} if device_id else {})):
        records=list(Reading.objects.filter(camera=c,timestamp__lt=end).order_by('timestamp','id'))
        prev=None; ins=outs=0; cb={}
        for r in records:
            if r.timestamp<start:
                prev=r;continue
            if r.mode=='delta': a,b=r.entries,r.exits
            elif prev is None or prev.mode!='cumulative':
                a=b=0;warnings.append(f'{c.name}: primeira leitura acumulada usada como referência; início sem cobertura')
            elif r.counter_epoch and prev.counter_epoch and r.counter_epoch!=prev.counter_epoch:
                a,b=r.entries,r.exits;warnings.append(f'{c.name}: novo ciclo do contador')
            elif r.entries<prev.entries or r.exits<prev.exits:
                a=b=0;warnings.append(f'{c.name}: contador diminuiu sem ciclo identificado; diferença não contabilizada')
            else:a,b=r.entries-prev.entries,r.exits-prev.exits
            if r.mode=='cumulative' and prev and prev.timestamp<start and (a or b):
                warnings.append(f'{c.name}: diferença atravessa meia-noite; atribuída ao horário da leitura')
            ins+=a;outs+=b
            local=r.timestamp.astimezone(ZONE); key=f'{local.hour:02d}:{(local.minute//15)*15:02d}'
            cb[key]=cb.get(key,0)+a+b;bins[key]=bins.get(key,0)+a+b;prev=r
        rows.append({'id':c.id,'name':c.name,'ip':c.ip,'entries':ins,'exits':outs,'balance':ins-outs,'passages':ins+outs,'last_seen':c.last_seen.isoformat() if c.last_seen else None,'online':bool(c.last_seen and (timezone.now()-c.last_seen).total_seconds()<180),'peak_15min':max(cb,key=cb.get) if cb and max(cb.values()) else None})
    chart=[{'time':f'{h:02d}:{m:02d}','passages':bins.get(f'{h:02d}:{m:02d}',0)} for h in range(24) for m in (0,15,30,45)]
    hours={f'{h:02d}:00':sum(x['passages'] for x in chart if int(x['time'][:2])==h) for h in range(24)}
    totals={k:sum(r[k] for r in rows) for k in ('entries','exits','balance','passages')}
    return {'date':str(day),'timezone':str(ZONE),'initial_balance':0,'cameras':rows,'totals':totals,'intervals':chart,'peak_15min':max(bins,key=bins.get) if bins and max(bins.values()) else None,'peak_hour':max(hours,key=hours.get) if max(hours.values()) else None,'warnings':sorted(set(warnings)),'peak_basis':'entradas + saídas; horário da leitura','updated_at':timezone.now().isoformat()}
def close_day(day):
    data=report(day)
    DailyReport.objects.update_or_create(day=day,defaults={'data':data})
    return data
