"""Entry-focused operational checks and comparable business periods."""
import calendar
from datetime import date,datetime,time,timedelta
from django.db.models import Sum,Count
from django.db.models.functions import TruncDate,TruncTime
from django.utils import timezone
from .models import Camera,Reading,PullCursor,HistoryImport,CollectionGap,CalendarException,AuditLog
from .services import ZONE,report

DONE=('completed','archived')
def closing_at(day):
    from .whatsapp import final_time
    return datetime.combine(day,final_time(day),ZONE)

def entries_day(day,now=None,clock=None):
    now=now or timezone.now()
    cutoff=min(now,closing_at(day))
    if clock:cutoff=min(cutoff,datetime.combine(day,clock,ZONE))
    return report(day,until=cutoff+timedelta(microseconds=1))

def day_check(day,now=None):
    now=now or timezone.now();today=now.astimezone(ZONE).date()
    data=entries_day(day,now);rows=[]
    for c in Camera.objects.filter(enabled=True,include_in_total=True).order_by('id'):
        relevant=Camera.objects.filter(ip=c.ip,enabled=True)
        jobs=HistoryImport.objects.filter(camera__in=relevant,start__lte=day,end__gte=day)
        gaps=CollectionGap.objects.filter(camera__in=relevant,started_at__lt=datetime.combine(day+timedelta(days=1),time.min,ZONE)).exclude(state='recovered').filter(ended_at__isnull=True)|CollectionGap.objects.filter(camera__in=relevant,started_at__lt=datetime.combine(day+timedelta(days=1),time.min,ZONE),ended_at__gte=datetime.combine(day,time.min,ZONE)).exclude(state='recovered')
        blockers=[]
        if jobs.exclude(status__in=DONE).exists():blockers.append('Importação pendente, interrompida ou com falha')
        if gaps.exists():blockers.append('Falha de comunicação ainda sem recuperação concluída')
        source=relevant.order_by('replica_slot','id').first()
        cursor=PullCursor.objects.filter(camera=source,initialized=True).first()
        if day==today and (not cursor or cursor.last_error or not cursor.last_success or now-cursor.last_success>timedelta(minutes=3)):
            blockers.append('Consulta direta sem confirmação recente')
        item=next((x for x in data['cameras'] if x['id']==c.pk),None)
        if not item:continue
        if day<today and not jobs.filter(status='completed').exists():blockers.append('Sem reexame histórico completo registrado para este dia')
        if Reading.objects.filter(camera=c,timestamp__gte=datetime.combine(day,time.min,ZONE),timestamp__lt=datetime.combine(day+timedelta(days=1),time.min,ZONE)).exclude(mode='delta').exists():blockers.append('Fonte acumulada exige conferência das referências')
        rows.append({'id':c.pk,'name':c.name,'entries':item['entries'],'status':'Revisar' if blockers else 'Pronto para conferência','blockers':blockers,'history_scanned':jobs.filter(status='completed').exists(),'replica':bool(c.replica_slot)})
    audit=AuditLog.objects.filter(action='whatsapp_verified',object_id=str(day)).order_by('-at').first()
    return {'day':day,'cutoff':closing_at(day),'partial':now<closing_at(day),'entries':data['totals']['entries'],'devices':rows,'blocked':not rows or any(r['blockers'] for r in rows),'last_verified':audit,'notice':'Pronto para conferência não significa cobertura certificada. Ausência de passagens não indica falha de comunicação. O reexame alcança somente o buffer ainda disponível no equipamento.'}

def day_type(day):
    special=CalendarException.objects.filter(day=day).first()
    if special:
        if special.closed:return 'Fechado'
        if special.is_holiday:return 'Feriado'
        return 'Especial'
    return 'Domingo' if day.weekday()==6 else 'Dia útil / sábado'

def comparable_days(day,count=4):
    kind=day_type(day);found=[]
    begin=day-timedelta(days=366)
    exceptions={x.day:x for x in CalendarException.objects.filter(day__gte=begin,day__lte=day)}
    available=set(Reading.objects.filter(camera__enabled=True,camera__include_in_total=True,timestamp__gte=datetime.combine(begin,time.min,ZONE),timestamp__lt=datetime.combine(day,time.min,ZONE)).annotate(d=TruncDate('timestamp',tzinfo=ZONE)).values_list('d',flat=True).distinct())
    def category(d):
        special=exceptions.get(d)
        if special:return 'Fechado' if special.closed else 'Feriado' if special.is_holiday else 'Especial'
        return 'Domingo' if d.weekday()==6 else 'Dia útil / sábado'
    for candidate in sorted(available,reverse=True):
        if category(candidate)!=kind:continue
        if kind=='Dia útil / sábado' and candidate.weekday()!=day.weekday():continue
        found.append(candidate)
        if len(found)>=count:break
    return found

def comparable_analysis(day,now=None):
    now=now or timezone.now();local=now.astimezone(ZONE)
    clock=local.time().replace(tzinfo=None) if day==local.date() and now<closing_at(day) else None
    current=entries_day(day,now,clock);samples=[]
    for other in comparable_days(day):
        d=entries_day(other,now,clock)
        samples.append({'day':other,'entries':d['totals']['entries'],'quality':day_check(other,now)['blocked']})
    avg=round(sum(s['entries'] for s in samples)/len(samples),1) if samples else None
    return {'day':day,'kind':day_type(day),'entries':current['totals']['entries'],'samples':samples,'average':avg,'variation':round((current['totals']['entries']-avg)/avg*100,1) if avg else None,'partial':clock is not None,'clock':clock,'quality':day_check(day,now)}

def flow_month(reference,now=None):
    now=now or timezone.now();today=now.astimezone(ZONE).date()
    start=reference.replace(day=1);end=start.replace(day=calendar.monthrange(start.year,start.month)[1])
    q=Reading.objects.filter(camera__enabled=True,camera__include_in_total=True,timestamp__gte=datetime.combine(start,time.min,ZONE),timestamp__lt=datetime.combine(end+timedelta(days=1),time.min,ZONE),timestamp__lte=now).annotate(clock=TruncTime('timestamp',tzinfo=ZONE)).filter(clock__lte=time(22,10))
    values={r['day']:r for r in q.annotate(day=TruncDate('timestamp',tzinfo=ZONE)).values('day').annotate(entries=Sum('entries'),records=Count('id'))}
    # Respect the operational 21h close on Sundays/registered holidays too.
    points=[]
    for n in range(end.day):
        day=start+timedelta(days=n);item=values.get(day);future=day>today
        pending=HistoryImport.objects.filter(camera__enabled=True,camera__include_in_total=True,start__lte=day,end__gte=day).exclude(status__in=DONE).exists()
        mixed=q.filter(timestamp__gte=datetime.combine(day,time.min,ZONE),timestamp__lt=datetime.combine(day+timedelta(days=1),time.min,ZONE)).exclude(mode='delta').exists()
        entries=entries_day(day,now)['totals']['entries'] if item and (mixed or closing_at(day).time()!=time(22,10)) else (item['entries'] if item else None)
        points.append({'day':day,'number':day.day,'entries':entries,'has_data':item is not None,'future':future,'pending':pending,'kind':day_type(day),'partial':day==today and now<closing_at(day)})
    maximum=max([p['entries'] or 0 for p in points]+[1])
    for p in points:p['level']=min(4,max(1,int((p['entries'] or 0)/maximum*4))) if p['has_data'] else 0
    weeks=[];offset=start.weekday();grid=[None]*offset+points
    grid += [None]*((-len(grid))%7)
    for n in range(0,len(grid),7):weeks.append(grid[n:n+7])
    return {'start':start,'end':end,'weeks':weeks,'entries':sum(p['entries'] or 0 for p in points),'days_with_data':sum(p['has_data'] for p in points)}

def weekly_executive(start,now=None):
    now=now or timezone.now();days=[];devices={};peak={};total=0
    for n in range(7):
        day=start+timedelta(days=n);d=entries_day(day,now)
        a=d['totals']['entries'];total+=a
        days.append({'day':str(day),'entries':a,'kind':day_type(day),'blocked':day_check(day,now)['blocked']})
        for c in d['cameras']:
            if c['include_in_total']:devices[c['name']]=devices.get(c['name'],0)+c['entries']
        for point in d['intervals']:peak[point['time']]=peak.get(point['time'],0)+point['entries']
    previous=sum(entries_day(start-timedelta(days=7)+timedelta(days=n),now)['totals']['entries'] for n in range(7))
    return {'version':2,'entries':total,'previous_entries':previous,'variation':round((total-previous)/previous*100,1) if previous else None,'days':days,'devices':[{'name':name,'entries':value,'share':round(value/total*100,1) if total else 0} for name,value in sorted(devices.items(),key=lambda x:-x[1])],'peak':max(peak,key=peak.get) if peak and max(peak.values()) else None,'peak_entries':max(peak.values()) if peak else 0,'pending_days':sum(d['blocked'] for d in days),'basis':'Entradas até o fechamento de cada dia. Comparação com a semana anterior; feriados e dias especiais identificados, sem ajuste estatístico. Valores disponíveis, não certificados.'}
