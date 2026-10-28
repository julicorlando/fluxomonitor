import re

from django import forms

from .models import NetworkAsset


class NetworkAssetForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields['enabled'].initial = False

    class Meta:
        model = NetworkAsset
        fields = (
            'name', 'kind', 'site', 'ip', 'mac', 'manufacturer', 'model',
            'location', 'patrimony', 'protocol', 'username', 'credential_env',
            'verify_tls', 'enabled',
        )
        widgets = {
            'name': forms.TextInput(attrs={'placeholder': 'Ex.: Switch principal - Bloco A'}),
            'ip': forms.TextInput(attrs={'placeholder': '192.168.1.10'}),
            'mac': forms.TextInput(attrs={'placeholder': '00:11:22:33:44:55'}),
            'location': forms.TextInput(attrs={'placeholder': 'Prédio, andar, sala ou setor'}),
            'credential_env': forms.TextInput(attrs={'placeholder': 'NETCONTROL_SWITCH01_SECRET'}),
        }

    def clean_credential_env(self):
        name = self.cleaned_data.get('credential_env', '').strip()
        if name and not re.fullmatch(r'[A-Z][A-Z0-9_]{1,99}', name):
            raise forms.ValidationError('Use o nome de uma variável de ambiente em maiúsculas, por exemplo NETCONTROL_SWITCH01_SECRET.')
        return name

    def clean(self):
        data = super().clean()
        if data.get('protocol') in ('snmp', 'routeros') and not data.get('credential_env'):
            self.add_error('credential_env', 'Informe o nome da variável de ambiente que guardará o segredo.')
        if data.get('protocol') == 'routeros' and not data.get('username'):
            self.add_error('username', 'Informe o usuário de leitura do RouterOS.')
        return data
