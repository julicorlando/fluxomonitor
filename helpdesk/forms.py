from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from .identity import normalize_phone
from .models import AgentGrant, Company, Contact, Ticket


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
    status = forms.ChoiceField(label='Status', choices=Ticket.STATUS)
    text = forms.CharField(label='Atualização / solução', min_length=1, max_length=4000,
                           widget=forms.Textarea(attrs={'rows': 4}))
    internal = forms.BooleanField(label='Anotação interna (não enviar pelo WhatsApp)', required=False)
    assigned_to = forms.ModelChoiceField(label='Responsável', queryset=get_user_model().objects.none(), required=False)
    revision = forms.IntegerField(widget=forms.HiddenInput)

    def __init__(self, *args, ticket, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['assigned_to'].queryset = get_user_model().objects.filter(is_active=True, is_staff=True).filter(
            Q(is_superuser=True) | Q(agentgrant__company=ticket.company)).distinct().order_by('username')
        from .services import TRANSITIONS
        self.fields['status'].choices = [(key, label) for key, label in Ticket.STATUS if key == ticket.status or key in TRANSITIONS[ticket.status]]
        self.initial.update({'status': ticket.status, 'revision': ticket.revision, 'assigned_to': ticket.assigned_to_id})


class GrantForm(forms.Form):
    user = forms.ModelChoiceField(label='Operador do GesTi', queryset=get_user_model().objects.filter(is_active=True, is_staff=True, is_superuser=False).order_by('username'))
    companies = forms.ModelMultipleChoiceField(label='Empresas permitidas', queryset=Company.objects.all(), required=False,
                                               widget=forms.CheckboxSelectMultiple)
