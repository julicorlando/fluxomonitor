import calendar,hashlib,secrets
from datetime import date,datetime,time,timedelta
from django import forms
from django.contrib.auth.decorators import login_required,user_passes_test
from django.db.models import Sum
from django.db import transaction
from django.db.models.functions import TruncHour,TruncDate,TruncMonth,TruncTime
from django.http import HttpResponse,JsonResponse
from django.shortcuts import render,redirect
from django.utils import timezone
from .models import Camera,Reading,PublicLink,AuditLog,HistoryImport
from .services import ZONE

def protected(response,admin_form=False):
    response['Cache-Control']='private, no-store, max-age=0'
    # HTTPS form submissions need same-origin provenance for Django CSRF.
    # Public token URLs must still never be sent as referrers.
    response['Referrer-Policy']='same-origin' if admin_form else 'no-referrer'
    response['X-Robots-Tag']='noindex, nofollow, noarchive'
    response['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
    return response

class LinkForm(forms.ModelForm):
    duration_days=forms.IntegerField(label='Validade em dias',min_value=1,max_value=365,initial=7,help_text='De 1 a 365 dias. Pode prorrogar o mesmo link ou revogar a qualquer momento.')
    class Meta:
        model=PublicLink
        fields=['label']
        labels={'label':'Nome do link'}


@login_required
@user_passes_test(lambda u:u.is_superuser)
def manage(request):
    url=None;extension_error=None
    form=LinkForm()
    if request.method=='POST':
        if request.POST.get('action')=='extend':
            try:
                days=int(request.POST.get('days','0'))
                if not 1<=days<=365:raise ValueError()
                with transaction.atomic():
                    link=PublicLink.objects.select_for_update().filter(pk=request.POST.get('link_id'),revoked_at__isnull=True).first()
                    if not link:raise ValueError()
                    link.expires_at=max(timezone.now(),link.expires_at)+timedelta(days=days)
                    link.save(update_fields=['expires_at'])
                    AuditLog.objects.create(actor=request.user.get_username(),action='public_link_extend',object_type='PublicLink',object_id=str(link.pk),changes={'added_days':days,'expires_at':link.expires_at.isoformat()})
                return redirect('/public-links/')
            except (ValueError,OverflowError):extension_error='Informe de 1 a 365 dias para um link que não esteja revogado.'
        elif request.POST.get('action')=='revoke':
            link=PublicLink.objects.filter(pk=request.POST.get('link_id'),revoked_at__isnull=True).first()
            if link:
                link.revoked_at=timezone.now();link.save(update_fields=['revoked_at'])
                AuditLog.objects.create(actor=request.user.get_username(),action='public_link_revoke',object_type='PublicLink',object_id=str(link.pk),changes={})
            return redirect('/public-links/')
        form=LinkForm(request.POST) if not extension_error else form
        if not extension_error and form.is_valid():
            token=secrets.token_urlsafe(32);link=form.save(commit=False)
            # Legacy date columns are retained for database compatibility only.
            link.start=date(2000,1,1);link.end=date(9999,12,31)
            link.digest=hashlib.sha256(token.encode()).hexdigest();link.actor=request.user.get_username()
            link.expires_at=timezone.now()+timedelta(days=form.cleaned_data['duration_days']);link.save()
            url=request.build_absolute_uri('/public/'+token+'/')
            AuditLog.objects.create(actor=link.actor,action='public_link_create',object_type='PublicLink',object_id=str(link.pk),changes={'history':'all_available','daily_cutoff':'22:10','metric':'entries','expires_at':link.expires_at.isoformat()})
    return protected(render(request,'public_links.html',{'form':form,'public_url':url,'extension_error':extension_error,'links':PublicLink.objects.select_related('camera').order_by('-id')[:100]}),admin_form=True)

class CompareForm(forms.Form):
    mode=forms.ChoiceField(label='Comparar por',choices=[('day','Dia a dia'),('month','Mês a mês'),('year','Ano a ano'),('range','Período personalizado')])
    reference=forms.DateField(label='Data de referência',widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    end=forms.DateField(label='Data final (período personalizado)',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    compare_end=forms.DateField(label='Fim da comparação (período personalizado)',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    compare=forms.DateField(label='Comparar com (opcional)',required=False,widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))

def period(mode,reference,link,end_date=None):
    today=timezone.now().astimezone(ZONE).date()
    if mode=='year':start=date(reference.year,1,1);end=date(reference.year,12,31)
    elif mode=='month':start=reference.replace(day=1);end=reference.replace(day=calendar.monthrange(reference.year,reference.month)[1])
    elif mode=='range':
        if not end_date:raise ValueError('Informe a data final do período personalizado.')
        start=reference;end=end_date
    else:start=end=reference
    end=min(end,today)
    if start<date(2000,1,1) or start>end:raise ValueError('Informe um período válido, de 2000 até hoje.')
    return start,end

def analytics(link,mode,reference,end_date=None):
    start,end=period(mode,reference,link,end_date)
    begin=datetime.combine(start,time.min,tzinfo=ZONE);finish=datetime.combine(end+timedelta(days=1),time.min,tzinfo=ZONE)
    cameras=Camera.objects.filter(enabled=True)
    cameras=cameras.filter(pk=link.camera_id) if link.camera_id else cameras.filter(include_in_total=True)
    # Apply the closing time to EVERY local day, not only the end of the range.
    now=timezone.now()
    readings=Reading.objects.filter(camera__in=cameras,timestamp__gte=begin,timestamp__lt=finish,timestamp__lte=now).annotate(local_time=TruncTime('timestamp',tzinfo=ZONE)).filter(local_time__lte=time(22,10))
    if readings.exclude(mode='delta').exists():raise ValueError('Há contadores acumulados neste período; normalize os eventos antes da consulta BI pública.')
    trunc={'day':TruncHour,'month':TruncDate,'year':TruncMonth,'range':TruncDate}[mode]
    grouped=list(readings.annotate(bucket=trunc('timestamp',tzinfo=ZONE)).values('bucket').annotate(entries=Sum('entries')).order_by('bucket'))
    points=[];totals={'entries':0}
    for row in grouped:
        key=row['bucket'];label=key.strftime('%H:%M' if mode=='day' else '%d' if mode=='month' else '%m' if mode=='year' else '%d/%m/%Y')
        if mode=='range':label=f'Dia {(key-start).days+1:05d}'
        a=row['entries'];totals['entries']+=a
        points.append({'label':label,'entries':a})
    names=list(cameras.values_list('name',flat=True))
    present=set(readings.values_list('camera_id',flat=True).distinct())
    missing=list(cameras.exclude(pk__in=present).values_list('name',flat=True))
    pending=HistoryImport.objects.filter(camera__in=cameras,start__lte=end,end__gte=start).exclude(status__in=['completed','archived']).exists()
    warnings=[]
    if missing:warnings.append('Sem registros no período: '+', '.join(missing))
    if pending:warnings.append('Há importações não concluídas neste período; os totais podem mudar.')
    return {'start':str(start),'end':str(end),'totals':totals,'points':points,'has_data':bool(grouped),'devices':names,'warnings':warnings,'pending_imports':pending,'daily_cutoff':'22:10','partial':end==now.astimezone(ZONE).date() and now.astimezone(ZONE).time()<time(22,10)}

def public(request,token,as_json=False):
    if len(token)>100:return protected(HttpResponse('Link indisponível.',status=404))
    link=PublicLink.objects.filter(digest=hashlib.sha256(token.encode()).hexdigest(),revoked_at__isnull=True,expires_at__gt=timezone.now()).first()
    if not link:return protected(HttpResponse('Link expirado, revogado ou inexistente.',status=404))
    initial={'mode':'day','reference':str(timezone.now().astimezone(ZONE).date())}
    form=CompareForm(request.GET or initial);data=None;comparison=None;error=None;rows=[];variation=None
    if form.is_valid():
        try:
            mode=form.cleaned_data['mode'];data=analytics(link,mode,form.cleaned_data['reference'],form.cleaned_data.get('end'))
            if form.cleaned_data.get('compare'):comparison=analytics(link,mode,form.cleaned_data['compare'],form.cleaned_data.get('compare_end'))
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
