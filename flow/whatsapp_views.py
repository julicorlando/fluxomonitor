import re
from datetime import time,timedelta
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required,user_passes_test
from django.http import HttpResponse
from django.shortcuts import render,redirect
from django.views.decorators.http import require_POST
from .final_replays import preview as final_replay_preview,enqueue as enqueue_final_replay
from .models import WhatsAppConfig,WhatsAppDelivery,HistoryImport,Camera,AuditLog
from django.utils import timezone
from .services import report,ZONE
from .adjustments import adjusted_entries
from .intelligence import entries_day
from .whatsapp import gateway,start_session,session_info,list_groups,send_test,recipients,daily_times,flow_message,final_time

class ConfigForm(forms.ModelForm):
    send_count=forms.IntegerField(label='Quantidade de envios por dia',min_value=1,max_value=24)
    times=forms.CharField(label='Horários dos envios (Brasília)',widget=forms.Textarea(attrs={'rows':3}),help_text='Um horário HH:MM por linha ou separado por vírgula. Ex.: 12:00, 18:00, 22:10.')
    number=forms.CharField(label='Números com país e DDD',required=False,widget=forms.Textarea(attrs={'rows':3}),help_text='Separe por vírgula ou linha. Ex.: 5581999999999, 5581888888888.')
    group=forms.MultipleChoiceField(label='Grupos (pode selecionar vários)',required=False,widget=forms.CheckboxSelectMultiple)
    class Meta:
        model=WhatsAppConfig
        fields=['enabled','error_margin_percent']
        labels={'enabled':'Ativar envio diário','metric':'Qual soma enviar','send_at':'Horário (Brasília)'}
        widgets={'error_margin_percent':forms.NumberInput(attrs={'min':'0','max':'100','step':'0.01'}),'send_at':forms.TimeInput(attrs={'type':'time'})}
    def __init__(self,*a,groups=None,**kw):
        super().__init__(*a,**kw)
        # Preserve the saved percentage for older browser tabs that omit the new field.
        if self.is_bound and 'error_margin_percent' not in self.data:
            self.data=self.data.copy();self.data['error_margin_percent']=str(self.instance.error_margin_percent)
        choices=list(groups or [])
        known={k for k,v in choices}
        choices.extend((c,c) for c in recipients(self.instance) if c.endswith('@g.us') and c not in known)
        self.fields['group'].choices=choices
        times=self.instance.send_times or [str(self.instance.send_at)[:5]]
        self.fields['send_count'].initial=len(times)
        self.fields['times'].initial=', '.join(times)
    def clean(self):
        d=super().clean()
        numbers=[re.sub(r'[ +()\-]','',v.strip()) for v in re.split(r'[,;\n]',d.get('number','')) if v.strip()]
        if any(not re.fullmatch(r'[1-9][0-9]{7,14}',n) for n in numbers):self.add_error('number','Número inválido. Inclua país e DDD.')
        chats=list(dict.fromkeys([n+'@c.us' for n in numbers]+d.get('group',[])))
        if len(chats)>50:raise forms.ValidationError('Configure no máximo 50 destinatários.')
        if d.get('enabled') and not chats:raise forms.ValidationError('Defina pelo menos um destinatário antes de ativar.')
        chat=chats[0] if chats else ''
        self.instance.chat_ids=chats
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
    cfg,created=WhatsAppConfig.objects.get_or_create(pk=1)
    if created:cfg.refresh_from_db()
    error=None;status='Não configurado';groups=[]
    try:
        status=session_info().get('status','Desconhecido')
        if status=='WORKING':groups=list_groups()
    except Exception:error='Gateway indisponível ou sessão ainda não criada. Confira WAHA_API_KEY e o serviço waha.'
    initial={'number':', '.join(c.removesuffix('@c.us') for c in recipients(cfg) if c.endswith('@c.us')), 'group':[c for c in recipients(cfg) if c.endswith('@g.us')]}
    old_margin=str(cfg.error_margin_percent)
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
            cfg=form.save(commit=False);cfg.chat_id=form.cleaned_data['chat_id'];cfg.recipient_label=', '.join(dict(groups).get(c,c) for c in cfg.chat_ids)[:150];cfg.save()
            if old_margin!=str(cfg.error_margin_percent):AuditLog.objects.create(actor=request.user.get_username(),action='margin_changed',object_type='WhatsAppConfig',object_id='1',changes={'before':old_margin,'after':str(cfg.error_margin_percent)})
            return redirect('/whatsapp/')
    cfg.refresh_from_db()
    local=timezone.now().astimezone(ZONE);day=local.date()
    entries=entries_day(day)['totals']['entries']
    adjustment=adjusted_entries(entries,cfg.error_margin_percent)
    included=list(Camera.objects.filter(enabled=True,include_in_total=True))
    recent=sum(bool(c.last_seen and (timezone.now()-c.last_seen).total_seconds()<=max(180,c.expected_interval*3)) for c in included)
    pending_imports=HistoryImport.objects.filter(camera__ip__in=[c.ip for c in included],start__lte=day,end__gte=day).exclude(status__in=['completed','archived']).exists()
    schedule=[]
    for value in daily_times(cfg,day):
        slot=time.fromisoformat(value)
        schedule.append({'time':value,'final':slot==final_time(day),'past':slot<local.time().replace(tzinfo=None)})
    future=[item for item in schedule if not item['past']]
    next_send=future[0]['time']+' hoje' if future else daily_times(cfg,day+timedelta(days=1))[0]+' amanhã'
    try:final_replay=final_replay_preview(request.user.get_username(),labels=dict(groups))
    except ValueError as exc:final_replay={'rows':[],'token':'','error':str(exc)}
    from .models import WhatsAppFinalReplay
    replay_history=WhatsAppFinalReplay.objects.prefetch_related('deliveries').order_by('-created_at','-pk')[:10]
    response=render(request,'whatsapp.html',{'final_replay':final_replay,'replay_history':replay_history,'form':form,'cfg':cfg,'status':status,'error':error,'deliveries':WhatsAppDelivery.objects.order_by('-day','-scheduled_time','-id')[:30],'adjustment':adjustment,'preview':flow_message(local,adjustment['adjusted']),'preview_entries':entries,'schedule':schedule,'next_send':next_send,'recipient_count':len(recipients(cfg)),'recent':recent,'included_count':len(included),'pending_imports':pending_imports,'last_verified':AuditLog.objects.filter(action='whatsapp_verified').order_by('-at').first()})
    response['Cache-Control']='no-store';return response

@login_required
@user_passes_test(lambda u:u.is_superuser)
@require_POST
def replay_finals(request):
    try:
        replay,created=enqueue_final_replay(request.POST.get('preview_token',''),request.user.get_username(),destination=request.POST.get('destination',''),number=request.POST.get('number',''))
        messages.success(request,f'Reenvio #{replay.pk} '+('registrado na fila somente para o destino escolhido. Acompanhe o histórico abaixo.' if created else 'já foi registrado. O clique repetido não criou outro envio.'))
    except ValueError as exc:messages.error(request,str(exc))
    except Exception as exc:
        import logging
        from .notifications import runtime_failure
        logging.getLogger(__name__).exception('Falha ao registrar reenvio dos fluxos finais')
        runtime_failure('final_replay_request',exc)
        messages.error(request,'Não foi possível confirmar a solicitação de reenvio. Confira o histórico antes de solicitar novamente. A falha foi registrada para verificação.')
    return redirect('/whatsapp/#final-replays')
@login_required
@user_passes_test(lambda u:u.is_superuser)
def qr(request):
    try:raw=gateway('GET','/api/default/auth/qr?format=image',binary=True)
    except Exception:return HttpResponse('QR ainda indisponível. Atualize após iniciar a conexão.',status=503)
    response=HttpResponse(raw,content_type='image/png');response['Cache-Control']='no-store';return response
