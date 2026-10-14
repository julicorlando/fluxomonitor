import re
from django import forms
from django.contrib.auth.decorators import login_required,user_passes_test
from django.http import HttpResponse
from django.shortcuts import render,redirect
from .models import WhatsAppConfig,WhatsAppDelivery
from .whatsapp import gateway,start_session,session_info,list_groups

class ConfigForm(forms.ModelForm):
    number=forms.CharField(label='Número com país e DDD, ex.: 5581999999999',required=False)
    group=forms.ChoiceField(label='Ou selecione um grupo',required=False)
    class Meta:
        model=WhatsAppConfig
        fields=['enabled','metric','send_at']
        labels={'enabled':'Ativar envio diário','metric':'Qual soma enviar','send_at':'Horário (Brasília)'}
        widgets={'send_at':forms.TimeInput(attrs={'type':'time'})}
    def __init__(self,*a,groups=None,**kw):
        super().__init__(*a,**kw);self.fields['group'].choices=[('','Nenhum')]+(groups or [])
    def clean(self):
        d=super().clean();n=re.sub(r'[ +()\-]','',d.get('number',''));g=d.get('group')
        if n and g:raise forms.ValidationError('Escolha número ou grupo, não ambos.')
        if n and not re.fullmatch(r'[1-9][0-9]{7,14}',n):raise forms.ValidationError('Número inválido. Inclua país e DDD.')
        chat=(n+'@c.us') if n else g
        if not chat and self.instance.chat_id:chat=self.instance.chat_id
        if d.get('enabled') and not chat:raise forms.ValidationError('Defina o destinatário antes de ativar.')
        d['chat_id']=chat;return d
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
