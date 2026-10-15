import calendar,hashlib,secrets
from datetime import date,datetime,time,timedelta
from django import forms
from django.contrib.auth.decorators import login_required,user_passes_test
from django.db.models import Sum,Min
from django.db.models.functions import TruncHour,TruncDate,TruncMonth
from django.http import HttpResponse,JsonResponse
from django.shortcuts import render,redirect
from django.utils import timezone
from .models import Camera,Reading,PublicLink,AuditLog
from .services import ZONE

def protected(response):
    response['Cache-Control']='private, no-store, max-age=0'
    response['Referrer-Policy']='no-referrer'
    response['X-Robots-Tag']='noindex, nofollow, noarchive'
    response['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    return response

class LinkForm(forms.ModelForm):
    duration=forms.IntegerField(label='Validade em minutos',min_value=1,max_value=43200,initial=60,help_text='De 1 minuto a 30 dias. Pode revogar a qualquer momento.')
    class Meta:
        model=PublicLink
        fields=['label','camera','start','end']
        labels={'label':'Nome do link','camera':'Dispositivo (vazio = todos incluídos na soma)','start':'Histórico permitido desde','end':'Histórico permitido até'}
        widgets={'start':forms.DateInput(attrs={'type':'date'}),'end':forms.DateInput(attrs={'type':'date'})}
    def clean(self):
        data=super().clean();start=data.get('start');end=data.get('end')
        if start and end and (start>end or start<date(2000,1,1) or end>timezone.now().astimezone(ZONE).date()):raise forms.ValidationError('Informe um período válido, de 2000 até hoje.')
        return data

@login_required
@user_passes_test(lambda u:u.is_superuser)
def manage(request):
    today=timezone.now().astimezone(ZONE).date();url=None
    earliest=Reading.objects.aggregate(first=Min('timestamp'))['first']
    form=LinkForm(initial={'start':earliest.astimezone(ZONE).date() if earliest else today,'end':today})
    if request.method=='POST':
        if request.POST.get('action')=='revoke':
            link=PublicLink.objects.filter(pk=request.POST.get('link_id'),revoked_at__isnull=True).first()
            if link:
                link.revoked_at=timezone.now();link.save(update_fields=['revoked_at'])
                AuditLog.objects.create(actor=request.user.get_username(),action='public_link_revoke',object_type='PublicLink',object_id=str(link.pk),changes={})
            return redirect('/public-links/')
        form=LinkForm(request.POST)
        if form.is_valid():
            token=secrets.token_urlsafe(32);link=form.save(commit=False)
            link.digest=hashlib.sha256(token.encode()).hexdigest();link.actor=request.user.get_username()
            link.expires_at=timezone.now()+timedelta(minutes=form.cleaned_data['duration']);link.save()
            url=request.build_absolute_uri('/public/'+token+'/')
            AuditLog.objects.create(actor=link.actor,action='public_link_create',object_type='PublicLink',object_id=str(link.pk),changes={'start':str(link.start),'end':str(link.end),'expires_at':link.expires_at.isoformat(),'camera_id':link.camera_id})
    return protected(render(request,'public_links.html',{'form':form,'public_url':url,'links':PublicLink.objects.select_related('camera').order_by('-id')[:100]}))

class CompareForm(forms.Form):
    mode=forms.ChoiceField(label='Comparar por',choices=[('day','Dia a dia'),('month','Mês a mês'),('year','Ano a ano')])
    reference=forms.DateField(label='Data de referência',widget=forms.DateInput(attrs={'type':'date'}))
    compare=forms.DateField(label='Comparar com (opcional)',required=False,widget=forms.DateInput(attrs={'type':'date'}))

def period(mode,reference,link):
    today=timezone.now().astimezone(ZONE).date()
    if mode=='year':start=date(reference.year,1,1);end=date(reference.year,12,31)
    elif mode=='month':start=reference.replace(day=1);end=reference.replace(day=calendar.monthrange(reference.year,reference.month)[1])
    else:start=end=reference
    end=min(end,today)
    if start<link.start or end>link.end or start>end:raise ValueError('O período escolhido está fora do histórico permitido neste link. Para mês/ano atual, os dados vão até hoje.')
    return start,end

def analytics(link,mode,reference):
    start,end=period(mode,reference,link)
    begin=datetime.combine(start,time.min,tzinfo=ZONE);finish=datetime.combine(end+timedelta(days=1),time.min,tzinfo=ZONE)
    cameras=Camera.objects.filter(enabled=True)
    cameras=cameras.filter(pk=link.camera_id) if link.camera_id else cameras.filter(include_in_total=True)
    readings=Reading.objects.filter(camera__in=cameras,timestamp__gte=begin,timestamp__lt=finish)
    if readings.exclude(mode='delta').exists():raise ValueError('Há contadores acumulados neste período; normalize os eventos antes da consulta BI pública.')
    trunc={'day':TruncHour,'month':TruncDate,'year':TruncMonth}[mode]
    grouped=list(readings.annotate(bucket=trunc('timestamp',tzinfo=ZONE)).values('bucket').annotate(entries=Sum('entries'),exits=Sum('exits')).order_by('bucket'))
    points=[];totals={'entries':0,'exits':0,'balance':0,'passages':0}
    for row in grouped:
        key=row['bucket'];label=key.strftime('%H:%M' if mode=='day' else '%d' if mode=='month' else '%m')
        a=row['entries'];b=row['exits'];totals['entries']+=a;totals['exits']+=b
        points.append({'label':label,'entries':a,'exits':b,'passages':a+b})
    totals['balance']=totals['entries']-totals['exits'];totals['passages']=totals['entries']+totals['exits']
    return {'start':str(start),'end':str(end),'totals':totals,'points':points,'has_data':bool(grouped),'devices':list(cameras.values_list('name',flat=True))}

def public(request,token,as_json=False):
    if len(token)>100:return protected(HttpResponse('Link indisponível.',status=404))
    link=PublicLink.objects.filter(digest=hashlib.sha256(token.encode()).hexdigest(),revoked_at__isnull=True,expires_at__gt=timezone.now()).first()
    if not link:return protected(HttpResponse('Link expirado, revogado ou inexistente.',status=404))
    initial={'mode':'day','reference':str(link.end)}
    form=CompareForm(request.GET or initial);data=None;comparison=None;error=None;rows=[];variation=None
    if form.is_valid():
        try:
            mode=form.cleaned_data['mode'];data=analytics(link,mode,form.cleaned_data['reference'])
            if form.cleaned_data.get('compare'):comparison=analytics(link,mode,form.cleaned_data['compare'])
            main={p['label']:p for p in data['points']};other={p['label']:p for p in comparison['points']} if comparison else {}
            top=max([1]+[p['entries'] for p in list(main.values())+list(other.values())])
            for label in sorted(main.keys()|other.keys()):
                a=main.get(label,{}).get('entries',0);b=other.get(label,{}).get('entries',0)
                rows.append({'label':label,'entries':a,'comparison':b,'height':round(a/top*100,1),'compare_height':round(b/top*100,1)})
            if comparison and comparison['totals']['entries']:
                variation=round(100*(data['totals']['entries']-comparison['totals']['entries'])/comparison['totals']['entries'],1)
        except ValueError as exc:error=str(exc)
    else:error='Verifique as datas e o tipo de comparação.'
    if as_json:
        return protected(JsonResponse({'error':error} if error else {'data':data,'comparison':comparison,'variation_percent':variation,'updated_at':timezone.now().isoformat()},status=400 if error else 200))
    return protected(render(request,'public_bi.html',{'link':link,'form':form,'data':data,'comparison':comparison,'rows':rows,'variation':variation,'error':error,'updated_at':timezone.now()}))
