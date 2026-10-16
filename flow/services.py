from datetime import datetime,time,timedelta
from zoneinfo import ZoneInfo
from django.db import transaction,connection
from django.db.models import Min
from django.utils import timezone
from .models import Camera,Reading,DailyReport
ZONE=ZoneInfo('America/Sao_Paulo')
def bounds(day):
    return datetime.combine(day,time.min,ZONE),datetime.combine(day+timedelta(days=1),time.min,ZONE)
def report(day,device_id=None,group_id=None,until=None):
    start,end=bounds(day); end=min(end,until) if until else end; rows=[]; bins={}; entry_bins={}; exit_bins={}; warnings=[]
    for c in Camera.objects.filter(enabled=True).filter(**({"id":device_id} if device_id else {})).filter(**({"group_id":group_id} if group_id else {})):
        records=list(Reading.objects.filter(camera=c,timestamp__gte=start,timestamp__lt=end).order_by('timestamp','id'))
        prev=Reading.objects.filter(camera=c,timestamp__lt=start).order_by('-timestamp','-id').first(); ins=outs=0; cb={}; ce={}; cx={}
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
            cb[key]=cb.get(key,0)+a+b;ce[key]=ce.get(key,0)+a;cx[key]=cx.get(key,0)+b
            if device_id or c.include_in_total:
                bins[key]=bins.get(key,0)+a+b;entry_bins[key]=entry_bins.get(key,0)+a;exit_bins[key]=exit_bins.get(key,0)+b
            prev=r
        daily=records
        cutoff=min(end,timezone.now())
        gaps=[]
        points=[start]+[r.timestamp for r in daily]+[cutoff]
        threshold=max(180,c.expected_interval*3)
        if start<cutoff:
            gaps=[{'start':a.isoformat(),'end':b.isoformat()} for a,b in zip(points,points[1:]) if (b-a).total_seconds()>threshold]
        if not daily:warnings.append(f'{c.name}: sem leituras no dia')
        elif gaps:warnings.append(f'{c.name}: períodos sem cobertura de leitura')
        rows.append({'location':c.location,'replica_slot':c.replica_slot,'expected_interval':c.expected_interval,'intervals':[{'time':f'{h:02d}:{m:02d}','entries':ce.get(f'{h:02d}:{m:02d}',0),'exits':cx.get(f'{h:02d}:{m:02d}',0)} for h in range(24) for m in (0,15,30,45)],'group_id':c.group_id,'include_in_total':c.include_in_total,'coverage_gaps':gaps,'coverage_status':'partial' if gaps or not daily else 'observed','id':c.id,'name':c.name,'ip':c.ip,'entries':ins,'exits':outs,'balance':ins-outs,'passages':ins+outs,'last_seen':c.last_seen.isoformat() if c.last_seen else None,'online':bool(c.last_seen and (timezone.now()-c.last_seen).total_seconds()<max(180,c.expected_interval*3)),'peak_15min':max(cb,key=cb.get) if cb and max(cb.values()) else None})
    chart=[{'time':f'{h:02d}:{m:02d}','passages':bins.get(f'{h:02d}:{m:02d}',0),'entries':entry_bins.get(f'{h:02d}:{m:02d}',0),'exits':exit_bins.get(f'{h:02d}:{m:02d}',0)} for h in range(24) for m in (0,15,30,45)]
    hours={f'{h:02d}:00':sum(x['passages'] for x in chart if int(x['time'][:2])==h) for h in range(24)}
    totals={k:sum(r[k] for r in rows if device_id or r["include_in_total"]) for k in ('entries','exits','balance','passages')}
    return {'date':str(day),'timezone':str(ZONE),'initial_balance':0,'cameras':rows,'totals':totals,'intervals':chart,'peak_15min':max(bins,key=bins.get) if bins and max(bins.values()) else None,'peak_hour':max(hours,key=hours.get) if max(hours.values()) else None,'warnings':sorted(set(warnings)),'peak_basis':'entradas + saídas; horário da leitura','updated_at':timezone.now().isoformat()}
def close_day(day,reason='Atualização das leituras'):
    with transaction.atomic():
        # Different cameras can import the same day concurrently. Serialize
        # aggregate reads/writes so the last snapshot includes committed peers.
        if connection.vendor=='postgresql':
            with connection.cursor() as cursor:
                # One aggregate lock avoids lock-order inversions for batches
                # spanning several dates. Network collection stays parallel.
                cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)',[77816,0])
        data=report(day)
        old=DailyReport.objects.filter(day=day).first()
        def snapshot(value):
            return {'totals':value['totals'],'devices':{str(c['id']):{k:c[k] for k in ('entries','exits','passages')} for c in value['cameras']}}
        if old and snapshot(old.data)!=snapshot(data):
            from .models import ReportRevision,AuditLog
            revision=ReportRevision.objects.create(day=day,before=snapshot(old.data),after=snapshot(data),reason=reason[:150])
            AuditLog.objects.create(actor='system',action='report_revision',object_type='ReportRevision',object_id=str(revision.pk),changes={'day':str(day),'before':old.data['totals'],'after':data['totals'],'reason':reason[:150]})
        if not old or old.data!=data:DailyReport.objects.update_or_create(day=day,defaults={'data':data})
        return data
