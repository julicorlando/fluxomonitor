import csv,hashlib,secrets,json
from datetime import date,timedelta
from django import forms
from django.contrib.auth.decorators import login_required,user_passes_test
from django.contrib.auth.models import User,Group,Permission
from django.contrib.auth.password_validation import validate_password
from django.http import HttpResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from .models import Camera,ApiKey,RawDelivery,Reading,DeviceGroup,FlowEvent
from .services import report

class TokenForm(forms.Form):
    name=forms.CharField(label='Nome',max_length=100)
    camera=forms.ModelChoiceField(label='Dispositivo (vazio = todos)',queryset=Camera.objects.all(),required=False)
    can_read=forms.BooleanField(label='Permitir consultas',required=False,initial=True)
    can_ingest=forms.BooleanField(label='Permitir recebimento',required=False)
    def clean(self):
        data=super().clean()
        if not data.get('can_read') and not data.get('can_ingest'):raise forms.ValidationError('Selecione uma permissão.')
        return data
@login_required
@user_passes_test(lambda u:u.is_superuser)
def tokens(request):
    token=None;form=TokenForm(request.POST or None)
    if request.method=='POST':
        if request.POST.get('revoke'):
            ApiKey.objects.filter(pk=request.POST['revoke']).update(enabled=False);return redirect('/tokens/')
        if form.is_valid():
            token=secrets.token_urlsafe(32)
            ApiKey.objects.create(**form.cleaned_data,digest=hashlib.sha256(token.encode()).hexdigest())
            form=TokenForm()
    response=render(request,'tokens.html',{'form':form,'token':token,'keys':ApiKey.objects.select_related('camera')})
    response['Cache-Control']='no-store';return response
@login_required
@user_passes_test(lambda u:u.is_staff)
def groups(request):
    form=forms.Form(request.POST or None)
    form.fields['name']=forms.CharField(label='Nome do grupo',max_length=100)
    if request.method=='POST' and form.is_valid():DeviceGroup.objects.get_or_create(name=form.cleaned_data['name']);return redirect('/groups/')
    return render(request,'groups.html',{'form':form,'groups':DeviceGroup.objects.all()})
@login_required
def detail(request,device_id):
    c=get_object_or_404(Camera,pk=device_id)
    return render(request,'dashboard.html',{'device':c})
@login_required
@user_passes_test(lambda u:u.is_staff)
def diagnostics(request):
    from .models import PullCursor
    rows=[]
    for c in Camera.objects.all():
        latest=RawDelivery.objects.filter(camera=c).order_by('-id').first()
        reading=Reading.objects.filter(camera=c).order_by('-timestamp').first()
        payload=latest.payload if latest else None
        def redact(v):
            if isinstance(v,dict):return {k:('[oculto]' if any(x in k.lower() for x in ('password','token','sign','secret','authorization')) else redact(x)) for k,x in v.items()}
            if isinstance(v,list):return [redact(x) for x in v]
            return v
        rows.append({'pull':PullCursor.objects.filter(camera=c).first(),'device':c,'latest':latest,'reading':reading,'payload':json.dumps(redact(payload),ensure_ascii=False,indent=2) if latest else 'Nenhum envio recebido','status':'Sem contato' if not latest else ('Formato pendente' if latest.status=='captured' else latest.status)})
    return render(request,'diagnostics.html',{'rows':rows})
class UserForm(forms.Form):
    username=forms.CharField(max_length=150,label='Usuário')
    password=forms.CharField(widget=forms.PasswordInput,label='Senha')
    role=forms.ChoiceField(choices=[('viewer','Consulta'),('operator','Operador'),('admin','Administrador')],label='Perfil')
    def clean_username(self):
        name=self.cleaned_data['username']
        if User.objects.filter(username=name).exists():raise forms.ValidationError('Usuário já existe.')
        return name
    def clean_password(self):
        value=self.cleaned_data['password'];validate_password(value);return value
@login_required
@user_passes_test(lambda u:u.is_superuser)
def users(request):
    form=UserForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        d=form.cleaned_data;u=User.objects.create_user(d['username'],password=d['password'],is_staff=d['role']!='viewer',is_superuser=d['role']=='admin')
        if d['role']=='operator':
            g,_=Group.objects.get_or_create(name='Operadores')
            g.permissions.set(Permission.objects.filter(content_type__app_label='flow',content_type__model__in=['camera','devicegroup'],codename__regex='^(view|add|change)_'))
            u.groups.add(g)
        return redirect('/users/')
    return render(request,'users.html',{'form':form,'users':User.objects.all()})
@login_required
def reports(request):
    error=None;days=[]
    try:
        end=date.fromisoformat(request.GET.get('end',str(timezone.localdate())))
        start=date.fromisoformat(request.GET.get('start',str(end-timedelta(days=6))))
        if end<start or (end-start).days>92:raise ValueError()
        device_id=int(request.GET['device']) if request.GET.get('device') else None
        group_id=int(request.GET['group']) if request.GET.get('group') else None
        if device_id:get_object_or_404(Camera,pk=device_id)
        current=start
        while current<=end:days.append(report(current,device_id,group_id));current+=timedelta(days=1)
    except ValueError:error='Período inválido; máximo 93 dias.';start=end=timezone.localdate()
    if request.GET.get('format')=='csv' and not error:
        out=HttpResponse(content_type='text/csv; charset=utf-8');out['Content-Disposition']='attachment; filename="relatorio_fluxo.csv"';out.write('\ufeff')
        w=csv.writer(out,delimiter=';');w.writerow(['Data','Entradas','Saidas','Saldo','Passagens','Pico hora','Pico 15 min','Alertas'])
        for d in days:w.writerow([d['date'],*[d['totals'][k] for k in ('entries','exits','balance','passages')],d['peak_hour'],d['peak_15min'],' / '.join(d['warnings'])])
        return out
    ranking={}
    for d in days:
        for c in d['cameras']:
            item=ranking.setdefault(c['id'],{'name':c['name'],'passages':0,'included':c['include_in_total']});item['passages']+=c['passages']
    ranked=sorted(ranking.values(),key=lambda x:x['passages'],reverse=True)
    base=sum(x['passages'] for x in ranked)
    for x in ranked:x['share']=round(x['passages']*100/base,1) if base else 0
    events=FlowEvent.objects.filter(day__range=(start,end)).order_by('day')
    if request.GET.get('group'):events=events.filter(group_id=request.GET['group'])
    total=sum(d['totals']['passages'] for d in days)
    previous=[]
    if days:
        n=len(days)
        previous=[report(start-timedelta(days=i),device_id,group_id) for i in range(1,n+1)]
    prev_total=sum(d['totals']['passages'] for d in previous)
    qs=request.GET.copy();qs['format']='csv'
    return render(request,'reports.html',{'ranking':ranked,'events':events,'days':days,'start':start,'end':end,'error':error,'total':total,'previous':prev_total,'difference':total-prev_total,'csv_query':qs.urlencode(),'devices':Camera.objects.all(),'groups':DeviceGroup.objects.all()})

@login_required
@user_passes_test(lambda u:u.is_superuser)
def device_token(request,device_id):
    from django.db import transaction
    c=get_object_or_404(Camera,pk=device_id)
    if request.method!='POST':return HttpResponse('Use o botão Gerar token no dispositivo.',status=405)
    if not c.serial:return HttpResponse('Cadastre o número de série do dispositivo antes de gerar o token.',status=400)
    token=secrets.token_urlsafe(32)
    with transaction.atomic():
        c=Camera.objects.select_for_update().get(pk=c.pk)
        # Only replace tokens issued by this dedicated action. Other keys remain intact.
        name=f'push-device-{c.id}'
        for key in ApiKey.objects.filter(camera=c,name=name,enabled=True):
            key.enabled=False;key.save(update_fields=['enabled'])
        ApiKey.objects.create(name=name,camera=c,digest=hashlib.sha256(token.encode()).hexdigest(),can_ingest=True,can_read=False)
    response=render(request,'device_token.html',{'device':c,'token':token,'data_path':'/api/camera/dataUpload/'+token,'heartbeat_path':'/api/camera/heartBeat/'+token})
    response['Cache-Control']='no-store';response['Referrer-Policy']='no-referrer'
    return response
