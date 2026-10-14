import re
from datetime import time
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required,user_passes_test
from django.http import HttpResponse
from django.shortcuts import render,redirect
from .models import WhatsAppConfig,WhatsAppDelivery
from .whatsapp import gateway,start_session,session_info,list_groups,send_test

class ConfigForm(forms.ModelForm):
    send_count=forms.IntegerField(label='Quantidade de envios por dia',min_value=1,max_value=24)
    times=forms.CharField(label='Horários dos envios (Brasília)',widget=forms.Textarea(attrs={'rows':3}),help_text='Um horário HH:MM por linha ou separado por vírgula. Ex.: 12:00, 18:00, 22:10.')
    number=forms.CharField(label='Número com país e DDD, ex.: 5581999999999',required=False)
    group=forms.ChoiceField(label='Ou selecione um grupo',required=False)
    class Meta:
        model=WhatsAppConfig
        fields=['enabled']
        labels={'enabled':'Ativar envio diário','metric':'Qual soma enviar','send_at':'Horário (Brasília)'}
        widgets={'send_at':forms.TimeInput(attrs={'type':'time'})}
    def __init__(self,*a,groups=None,**kw):
        super().__init__(*a,**kw);self.fields['group'].choices=[('','Nenhum')]+(groups or [])
        times=self.instance.send_times or [str(self.instance.send_at)[:5]]
        self.fields['send_count'].initial=len(times)
        self.fields['times'].initial=', '.join(times)
    def clean(self):
        d=super().clean();n=re.sub(r'[ +()\-]','',d.get('number',''));g=d.get('group')
        if n and g:raise forms.ValidationError('Escolha número ou grupo, não ambos.')
        if n and not re.fullmatch(r'[1-9][0-9]{7,14}',n):raise forms.ValidationError('Número inválido. Inclua país e DDD.')
        chat=(n+'@c.us') if n else g
        if not chat and self.instance.chat_id:chat=self.instance.chat_id
        if d.get('enabled') and not chat:raise forms.ValidationError('Defina o destinatário antes de ativar.')
        values=[v.strip() for v in re.split(r'[,;\n]',d.get('times','')) if v.strip()]
        if any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',v) for v in values):self.add_error('times','Use horários de 00:00 a 23:59 no formato HH:MM.')
        elif len(set(values))!=len(values):self.add_error('times','Os horários devem ser diferentes.')
        elif len(values)!=d.get('send_count'):self.add_error('times','Informe um horário para cada envio configurado.')
        elif not 1<=len(values)<=24:self.add_error('times','Configure de 1 a 24 horários.')
        else:
            self.instance.send_times=sorted(values)
            self.instance.send_at=time.fromisoformat(self.instance.send_times[0])
        d['chat_id']=chat;self.instance.metric='entries';return d
@login_required
@user_passes_test(lambda u:u.is_superuser)
def settings(request):
    cfg,_=WhatsAppConfig.objects.get_or_create(pk=1)
    error=None;status='Não configurado';groups=[]
    try:
        status=session_info().get('status','Desconhecido')
        if status=='WORKING':groups=list_groups()
    except Exception:error='Gateway indisponível ou sessão ainda não criada. Confira WAHA_API_KEY e o serviço waha.'
    initial={'number':cfg.chat_id.removesuffix('@c.us') if cfg.chat_id.endswith('@c.us') else '', 'group':cfg.chat_id if cfg.chat_id.endswith('@g.us') else ''}
    form=ConfigForm(request.POST or None,instance=cfg,groups=groups,initial=initial)
    if request.method=='POST':
        if request.POST.get('action')=='connect':
            try:start_session();return redirect('/whatsapp/')
            except Exception:error='Não foi possível iniciar a sessão. Verifique os logs do gateway.'
        elif request.POST.get('action')=='test':
            try:
                text=send_test(cfg,request.user.get_username())
                messages.success(request,'Teste aceito pelo gateway: '+text+' — confira no destinatário.')
                return redirect('/whatsapp/')
            except ValueError as exc:error=str(exc)
            except Exception:error='Não foi possível consultar o gateway. Verifique a conexão do WhatsApp.'
        elif request.POST.get('action')=='logout':
            try:gateway('POST','/api/sessions/default/logout',{});cfg.enabled=False;cfg.save();return redirect('/whatsapp/')
            except Exception:error='Não foi possível desconectar a sessão.'
        elif form.is_valid():
            cfg=form.save(commit=False);cfg.chat_id=form.cleaned_data['chat_id'];cfg.recipient_label=dict(groups).get(cfg.chat_id,cfg.chat_id);cfg.save();return redirect('/whatsapp/')
    response=render(request,'whatsapp.html',{'form':form,'cfg':cfg,'status':status,'error':error,'deliveries':WhatsAppDelivery.objects.order_by('-day')[:30]})
    response['Cache-Control']='no-store';return response
@login_required
@user_passes_test(lambda u:u.is_superuser)
def qr(request):
    try:raw=gateway('GET','/api/default/auth/qr?format=image',binary=True)
    except Exception:return HttpResponse('QR ainda indisponível. Atualize após iniciar a conexão.',status=503)
    response=HttpResponse(raw,content_type='image/png');response['Cache-Control']='no-store';return response
