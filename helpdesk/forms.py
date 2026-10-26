from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from .identity import normalize_phone
from .models import AgentGrant, Company, Contact, Ticket, SlaPolicy, CompanySlaConfig, SlaAgentProfile, Asset, Maintenance, KnowledgeArticle, Problem


class ContactForm(forms.ModelForm):
    phone = forms.CharField(label='Número do WhatsApp', max_length=25, widget=forms.TextInput(attrs={'type': 'tel'}), help_text='Brasil, DDD 81. Ex.: +55 81 99999-9999.')
    class Meta:
        model = Contact
        fields = ['company', 'name', 'phone', 'email', 'active']
        labels = {'company': 'Empresa', 'name': 'Nome completo', 'phone': 'Número do WhatsApp', 'email': 'E-mail', 'active': 'Cadastro ativo'}
        help_texts = {'phone': 'Brasil, DDD 81. Ex.: +55 81 99999-9999. Números não cadastrados são ignorados.'}
        widgets = {'phone': forms.TextInput(attrs={'type': 'tel', 'maxlength': 25}), 'email': forms.EmailInput()}

    def clean_phone(self):
        return normalize_phone(self.cleaned_data['phone'])


class UpdateForm(forms.Form):
    asset = forms.ModelChoiceField(label='Equipamento do inventário', queryset=Asset.objects.none(), required=False)
    problem = forms.ModelChoiceField(label='Recorrência / problema conhecido', queryset=Problem.objects.none(), required=False)
    priority = forms.ChoiceField(label='Prioridade', choices=SlaPolicy.PRIORITIES, required=False)
    category = forms.ChoiceField(label='Categoria', choices=Ticket.CATEGORIES, required=False)
    start_sla = forms.BooleanField(label='Iniciar SLA deste chamado agora', required=False,
        help_text='Para chamados antigos sem SLA. Não cria atrasos retroativos.')
    status = forms.ChoiceField(label='Status', choices=Ticket.STATUS)
    text = forms.CharField(label='Atualização / solução', min_length=1, max_length=4000,
                           widget=forms.Textarea(attrs={'rows': 4}))
    internal = forms.BooleanField(label='Anotação interna (não enviar pelo WhatsApp)', required=False)
    assigned_to = forms.ModelChoiceField(label='Responsável', queryset=get_user_model().objects.none(), required=False)
    revision = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, ticket, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['asset'].queryset = Asset.objects.filter(company=ticket.company)
        self.fields['problem'].queryset = Problem.objects.filter(company=ticket.company)
        self.initial.update({'asset':ticket.asset_id,'problem':ticket.problem_id})
        self.fields['assigned_to'].queryset = get_user_model().objects.filter(is_active=True, is_staff=True).filter(
            Q(is_superuser=True) | Q(agentgrant__company=ticket.company)).distinct().order_by('username')
        from .services import TRANSITIONS
        self.fields['status'].choices = [(key, label) for key, label in Ticket.STATUS if key == ticket.status or key in TRANSITIONS[ticket.status]]
        if ticket.sla_started_at or ticket.status not in ('open', 'working', 'waiting'):
            self.fields.pop('start_sla')
        self.initial.update({'priority': ticket.priority, 'category': ticket.category, 'status': ticket.status, 'revision': ticket.revision, 'assigned_to': ticket.assigned_to_id})


class GrantForm(forms.Form):
    user = forms.ModelChoiceField(label='Operador do GesTi', queryset=get_user_model().objects.filter(is_active=True, is_staff=True, is_superuser=False).order_by('username'))
    companies = forms.ModelMultipleChoiceField(label='Empresas permitidas', queryset=Company.objects.all(), required=False,
                                               widget=forms.CheckboxSelectMultiple)


class SlaPolicyForm(forms.ModelForm):
    class Meta:
        model = SlaPolicy
        fields = ['response_minutes', 'resolution_minutes']
        labels = {'response_minutes': 'Primeira resposta (minutos)', 'resolution_minutes': 'Resolução (minutos)'}

    def clean(self):
        data = super().clean()
        if data.get('response_minutes') and data.get('resolution_minutes') and data['response_minutes'] > data['resolution_minutes']:
            raise forms.ValidationError('O prazo de resolução deve ser igual ou maior que o da primeira resposta.')
        return data


class DashboardFilterForm(forms.Form):
    start = forms.DateField(label='De', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    end = forms.DateField(label='Até', required=False, widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        data = super().clean()
        if data.get('start') and data.get('end') and data['start'] > data['end']:
            raise forms.ValidationError('A data inicial deve ser anterior ou igual à final.')
        return data


class CompanySlaForm(forms.ModelForm):
    holiday_dates = forms.CharField(label='Feriados e dias sem expediente', required=False,
        help_text='Uma data por linha, no formato AAAA-MM-DD. Cadastre os feriados de cada ano.',
        widget=forms.Textarea(attrs={'rows':5,'placeholder':'2026-12-25'}))
    class Meta:
        model = CompanySlaConfig
        fields = ['mode','pause_waiting','alerts_enabled','warning_minutes','escalation_minutes','manager',
                  'weekly_enabled','weekly_day','weekly_time']
        labels = {'mode':'Contagem de prazo','pause_waiting':'Pausar SLA ao aguardar o solicitante',
            'alerts_enabled':'Ativar alertas de SLA','warning_minutes':'Avisar antes do vencimento (minutos de SLA)',
            'escalation_minutes':'Escalonar ao gestor após o atraso (minutos de SLA)', 'manager':'Gestor da empresa',
            'weekly_enabled':'Enviar relatório semanal ao gestor','weekly_day':'Dia do envio semanal','weekly_time':'Horário de envio (Brasília)'}
        widgets = {'weekly_time':forms.TimeInput(format='%H:%M', attrs={'type':'time'})}

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        from .services import company_ids
        self.fields['manager'].queryset = get_user_model().objects.filter(is_active=True,is_staff=True).filter(
            Q(is_superuser=True)|Q(agentgrant__company=self.instance.company)).distinct().order_by('username')
        self.initial['holiday_dates'] = '\n'.join(self.instance.holidays or [])
        self.days = []
        for day,label in enumerate(['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo']):
            intervals = self.instance.schedule.get(str(day),[])
            key = f'd{day}'
            self.fields[key+'_enabled'] = forms.BooleanField(label=label,required=False,initial=bool(intervals))
            defaults = [intervals[0][0] if intervals else '08:00',intervals[-1][1] if intervals else '18:00',
                        intervals[0][1] if len(intervals)>1 else None,intervals[1][0] if len(intervals)>1 else None]
            for suffix,caption,initial in zip(['start','end','break_start','break_end'],['Entrada','Saída','Início intervalo','Fim intervalo'],defaults):
                self.fields[key+'_'+suffix] = forms.TimeField(label=caption,required=False,initial=initial,
                    widget=forms.TimeInput(format='%H:%M',attrs={'type':'time'}))
            self.days.append({'label':label, 'enabled':self[key+'_enabled'], 'start':self[key+'_start'], 'end':self[key+'_end'],
                'break_start':self[key+'_break_start'], 'break_end':self[key+'_break_end']})

    def clean_holiday_dates(self):
        from datetime import date
        lines = self.cleaned_data['holiday_dates'].splitlines()
        try:
            values = sorted({date.fromisoformat(line.strip()).isoformat() for line in lines if line.strip()})
        except ValueError:
            raise forms.ValidationError('Use uma data válida por linha, no formato AAAA-MM-DD.')
        if len(values)>3660:
            raise forms.ValidationError('Cadastre no máximo 3.660 datas.')
        return values

    def clean(self):
        data = super().clean()
        schedule = {}
        for day in range(7):
            key = f'd{day}'
            if not data.get(key+'_enabled'):
                continue
            start,end = data.get(key+'_start'),data.get(key+'_end')
            a,b = data.get(key+'_break_start'),data.get(key+'_break_end')
            if not start or not end or start>=end:
                self.add_error(None,'Informe entrada e saída válidas em cada dia ativo, sem virar a meia-noite.')
                continue
            if bool(a) != bool(b) or (a and not (start<a<b<end)):
                self.add_error(None,'O intervalo precisa ter início e fim dentro do expediente.')
                continue
            schedule[str(day)] = [[start.strftime('%H:%M'),a.strftime('%H:%M')],[b.strftime('%H:%M'),end.strftime('%H:%M')]] if a else [[start.strftime('%H:%M'),end.strftime('%H:%M')]]
        if data.get('mode') == 'business' and not schedule:
            self.add_error(None,'Ative ao menos um dia de expediente.')
        manager = data.get('manager')
        if data.get('weekly_enabled') and (not manager or not SlaAgentProfile.objects.filter(user=manager,contact__active=True).exists()):
            self.add_error('manager','Vincule o WhatsApp do gestor antes de ativar o relatório semanal.')
        data['schedule'] = schedule
        return data

    def save(self,commit=True):
        self.instance.schedule = self.cleaned_data['schedule']
        self.instance.holidays = self.cleaned_data['holiday_dates']
        return super().save(commit=commit)


class SlaAgentForm(forms.ModelForm):
    class Meta:
        model = SlaAgentProfile
        fields = ['user','contact']
        labels = {'user':'Técnico ou gestor (usuário do GesTi)','contact':'Cadastro que receberá os avisos no WhatsApp'}
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['user'].queryset = get_user_model().objects.filter(is_active=True,is_staff=True).order_by('username')
        self.fields['contact'].queryset = Contact.objects.filter(active=True).order_by('name')
        if self.instance.pk:
            self.fields['user'].disabled = True


class ScopedForm(forms.ModelForm):
    def __init__(self,*args,companies,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['company'].queryset = companies
        if self.instance.pk:
            self.fields['company'].disabled = True


class AssetForm(ScopedForm):
    class Meta:
        model = Asset
        fields = ['company','tag','invoice_number','acquisition_date','name','kind','state','contact','serial','location','notes']
        labels = {'company':'Empresa','tag':'Patrimônio / identificação','invoice_number':'Número da NF','acquisition_date':'Data da aquisição','name':'Equipamento','kind':'Tipo','state':'Situação',
                  'contact':'Colaborador responsável','serial':'Número de série','location':'Localização','notes':'Observações'}
        widgets = {'notes':forms.Textarea(attrs={'rows':3}), 'acquisition_date':forms.DateInput(attrs={'type':'date'}, format='%Y-%m-%d')}
    def __init__(self,*args,companies,**kwargs):
        super().__init__(*args,companies=companies,**kwargs)
        company_id = self.instance.company_id or (self.data.get('company') if self.is_bound else None)
        self.fields['contact'].queryset = Contact.objects.filter(company__in=companies,active=True)
        if company_id and str(company_id).isdigit():
            self.fields['contact'].queryset = self.fields['contact'].queryset.filter(company_id=company_id)
    def clean(self):
        data = super().clean()
        if not self.instance.pk:
            for field in ('tag', 'invoice_number', 'acquisition_date'):
                if not data.get(field):
                    self.add_error(field, 'Obrigatório no cadastro de novo equipamento.')
        if data.get('contact') and data.get('company') and data['contact'].company_id != data['company'].pk:
            self.add_error('contact','O colaborador deve pertencer à empresa do equipamento.')
        return data


class MaintenanceForm(forms.ModelForm):
    class Meta:
        model = Maintenance
        fields = ['date','description','cost','next_date']
        labels = {'date':'Data da manutenção','description':'Serviço / solução aplicada','cost':'Custo (opcional)','next_date':'Próxima manutenção (opcional)'}
        widgets = {'date':forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'), 'next_date':forms.DateInput(attrs={'type':'date'},format='%Y-%m-%d'), 'description':forms.Textarea(attrs={'rows':3})}
    def clean(self):
        data = super().clean()
        if data.get('next_date') and data.get('date') and data['next_date']<data['date']:
            self.add_error('next_date','A próxima manutenção não pode ser anterior ao serviço registrado.')
        return data


class KnowledgeForm(ScopedForm):
    class Meta:
        model = KnowledgeArticle
        fields = ['company','title','category','keywords','procedure','published','bot_enabled']
        labels = {'company':'Empresa','title':'Título','category':'Categoria','keywords':'Palavras-chave (separadas por vírgula)',
            'procedure':'Procedimento aprovado','published':'Publicar na base da equipe','bot_enabled':'Permitir sugestão pelo bot'}
        widgets = {'procedure':forms.Textarea(attrs={'rows':8})}
    def clean(self):
        data = super().clean()
        if data.get('bot_enabled') and not data.get('published'):
            self.add_error('bot_enabled','Publique o artigo antes de disponibilizá-lo no bot.')
        if data.get('bot_enabled') and not data.get('keywords','').strip():
            self.add_error('keywords','Informe palavras-chave para o bot encontrar este procedimento.')
        return data


class ProblemForm(ScopedForm):
    class Meta:
        model = Problem
        fields = ['company','title','category','status','root_cause','workaround']
        labels = {'company':'Empresa','title':'Problema recorrente','category':'Categoria','status':'Situação',
                  'root_cause':'Causa raiz / diagnóstico','workaround':'Contorno / solução'}
        widgets = {'root_cause':forms.Textarea(attrs={'rows':4}), 'workaround':forms.Textarea(attrs={'rows':4})}
