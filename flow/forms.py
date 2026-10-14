from django import forms
from .models import Camera
class DeviceForm(forms.ModelForm):
    class Meta:
        model=Camera
        fields=['name','ip','serial','location','manufacturer','model','api_url','enabled','group','include_in_total','expected_interval']
        labels={'name':'Nome','ip':'IP do dispositivo','serial':'Número de série','location':'Localização','manufacturer':'Fabricante','model':'Modelo','api_url':'API web do equipamento','enabled':'Ativo'}
    def clean_serial(self):return self.cleaned_data['serial'] or None

    def clean_expected_interval(self):
        value=self.cleaned_data['expected_interval']
        if not 5<=value<=86400:raise forms.ValidationError('Use entre 5 e 86400 segundos.')
        return value
