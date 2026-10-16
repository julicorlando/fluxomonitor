from .adjustments import admin_adjustment
"""Read-only analysis of stored flow records. Never polls or changes devices."""
import calendar,csv
from datetime import date,datetime,time,timedelta
from django.db.models import Sum,Count
from django.db.models.functions import TruncMinute,TruncDate,TruncMonth
from django.http import JsonResponse,HttpResponse
from django.utils import timezone
from .models import Camera,Reading,HistoryImport
from .services import ZONE,report
from .views import access

METRICS=('entries','exits','balance','passages')
def shift_month(day,delta):
    n=day.year*12+day.month-1+delta;y,m=divmod(n,12)
    return date(y,m+1,min(day.day,calendar.monthrange(y,m+1)[1]))

def period(mode,reference):
    if mode=='day':return reference,reference
    if mode=='week':return reference-timedelta(days=6),reference
    if mode=='month':return reference.replace(day=1),reference.replace(day=calendar.monthrange(reference.year,reference.month)[1])
    if mode=='year':return date(reference.year,1,1),date(reference.year,12,31)
    raise ValueError('Escolha dia, semana, mês ou ano.')

def summarize(mode,start,end,cameras,cutoff,device=None,group=None,grain=15):
    begin=datetime.combine(start,time.min,tzinfo=ZONE)
    finish=min(datetime.combine(end+timedelta(days=1),time.min,tzinfo=ZONE),cutoff)
    q=Reading.objects.filter(camera__in=cameras,timestamp__gte=begin,timestamp__lt=finish)
    values={};totals={k:0 for k in METRICS}
    counts=dict(q.values('camera_id').annotate(n=Count('id')).values_list('camera_id','n'))
    legacy=q.exclude(mode='delta').exists()
    if legacy:
        # Retain the same epoch/reset rules as the operational dashboard.
        day=start
        while day<=end:
            d=report(day,device,group,until=finish)
            totals={k:totals[k]+d['totals'][k] for k in METRICS}
            if mode=='day':
                for p in d['intervals']:
                    h,m=map(int,p['time'].split(':'));key=f'{h:02d}:{(m//grain)*grain:02d}'
                    row=values.setdefault(key,{'entries':0,'exits':0})
                    for k in row:row[k]+=p[k]
            else:
                key=day.strftime('%m' if mode=='year' else '%Y-%m-%d')
                row=values.setdefault(key,{'entries':0,'exits':0})
                for k in row:row[k]+=d['totals'][k]
            day+=timedelta(days=1)
    else:
        trunc=TruncMinute if mode=='day' else TruncMonth if mode=='year' else TruncDate
        for row in q.annotate(bucket=trunc('timestamp',tzinfo=ZONE)).values('bucket').annotate(entries=Sum('entries'),exits=Sum('exits')):
            b=row['bucket']
            key=f'{b.hour:02d}:{(b.minute//grain)*grain:02d}' if mode=='day' else b.strftime('%m' if mode=='year' else '%Y-%m-%d')
            item=values.setdefault(key,{'entries':0,'exits':0})
            for k in item:item[k]+=row[k];totals[k]+=row[k]
        totals['balance']=totals['entries']-totals['exits'];totals['passages']=totals['entries']+totals['exits']
    keys=[]
    if mode=='day':keys=[f'{h:02d}:{m:02d}' for h in range(24) for m in range(0,60,grain)]
    elif mode=='year':keys=[f'{m:02d}' for m in range(1,13)]
    else:keys=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    points=[]
    for key in keys:
        item=values.get(key,{'entries':0,'exits':0});a,b=item['entries'],item['exits']
        stamp=datetime.combine(start,time.min,tzinfo=ZONE)+timedelta(hours=int(key[:2]),minutes=int(key[3:])) if mode=='day' else datetime.combine(date(start.year,int(key),1) if mode=='year' else date.fromisoformat(key),time.min,tzinfo=ZONE)
        points.append({'label':key if mode=='day' else key if mode=='year' else key[5:],'key':key,'entries':a,'exits':b,'passages':a+b,'future':stamp>=finish})
    missing=[c.name for c in cameras if c.pk not in counts]
    pending=HistoryImport.objects.filter(camera__in=cameras,start__lte=end,end__gte=start,status__in=['pending','running','paused','failed','cancelled']).exists()
    warnings=[]
    if not cameras:warnings.append('Nenhum dispositivo incluído neste filtro.')
    if missing:warnings.append('Sem registros no período: '+', '.join(missing))
    if pending:warnings.append('Há importações pendentes, pausadas ou com falha neste período.')
    return {'start':str(start),'end':str(end),'through':finish.isoformat(),'partial':finish<datetime.combine(end+timedelta(days=1),time.min,tzinfo=ZONE),'totals':totals,'points':points,'has_data':bool(counts),'readings':sum(counts.values()),'missing_devices':missing,'pending_imports':pending,'warnings':warnings,'coverage':'incomplete' if missing or pending or not cameras else 'unverified','legacy':legacy}

@access('read')
def insights(request):
    try:
        mode=request.GET.get('mode','day');today=timezone.now().astimezone(ZONE).date()
        ref=date.fromisoformat(request.GET.get('date',str(today)))
        if ref>today or ref.year<2000:raise ValueError('Escolha uma data de 2000 até hoje.')
        device=int(request.GET['device']) if request.GET.get('device') else None
        group=int(request.GET['group']) if request.GET.get('group') else None
        scope=request.api_key.camera_id if request.api_key else None
        if scope and device and scope!=device:return JsonResponse({'error':'Sem permissão'},status=403)
        device=scope or device
        grain=int(request.GET.get('grain','15'))
        if grain not in (15,60):raise ValueError('Intervalo inválido.')
        start,end=period(mode,ref)
        comparison=request.GET.get('comparison','previous')
        if request.GET.get('compare_date'):other=date.fromisoformat(request.GET['compare_date'])
        elif comparison=='week':other=ref-timedelta(days=7)
        elif comparison=='year':other=date(ref.year-1,ref.month,min(ref.day,calendar.monthrange(ref.year-1,ref.month)[1]))
        elif mode=='month':other=shift_month(ref,-1)
        elif mode=='year':other=date(ref.year-1,ref.month,min(ref.day,calendar.monthrange(ref.year-1,ref.month)[1]))
        else:other=ref-timedelta(days=7 if mode=='week' else 1)
        if other>today or other.year<1999:raise ValueError('Data de comparação inválida.')
        prev_start,prev_end=period(mode,other)
        qs=Camera.objects.filter(enabled=True)
        if device:qs=qs.filter(pk=device)
        else:qs=qs.filter(include_in_total=True)
        if group:qs=qs.filter(group_id=group)
        cameras=list(qs)
        now=timezone.now().astimezone(ZONE)
        current=summarize(mode,start,end,cameras,now,device,group,grain)
        # Compare equivalent elapsed time when the current period is still open.
        elapsed=datetime.fromisoformat(current['through'])-datetime.combine(start,time.min,tzinfo=ZONE)
        prev_cutoff=min(now,datetime.combine(prev_start,time.min,tzinfo=ZONE)+elapsed) if current['partial'] else now
        previous=summarize(mode,prev_start,prev_end,cameras,prev_cutoff,device,group,grain)
        if request.user.is_superuser:
            current['adjustment']=admin_adjustment(request,current['totals']['entries'])
            previous['adjustment']=admin_adjustment(request,previous['totals']['entries'])
        base=previous['totals']['entries'];difference=current['totals']['entries']-base
        if request.GET.get('format')=='csv':
            response=HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition']='attachment; filename="gesti_comparacao.csv"';response['Cache-Control']='private, no-store';response.write('\ufeff')
            writer=csv.writer(response,delimiter=';')
            writer.writerow(['Periodo','Inicio','Fim','Ate','Intervalo','Entradas','Saidas','Passagens','Cobertura'])
            for label,data in [('Principal',current),('Comparacao',previous)]:
                for point in data['points']:
                    if not point['future']:writer.writerow([label,data['start'],data['end'],data['through'],point['label'],point['entries'],point['exits'],point['passages'],' / '.join(data['warnings']) or 'Nao certificada'])
            return response
        response=JsonResponse({'current':current,'previous':previous,'difference':difference,'variation':round(difference/base*100,1) if base else None,'conclusive':False,'basis':'Registros disponíveis; cobertura não certificada. Períodos em andamento comparam o tempo já transcorrido.','updated_at':now.isoformat()})
        response['Cache-Control']='private, no-store';return response
    except (ValueError,OverflowError,TypeError):return JsonResponse({'error':'Filtros inválidos. Escolha um período de 2000 até hoje.'},status=400)
