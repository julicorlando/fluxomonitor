from django import forms
from .models import Camera
class DeviceForm(forms.ModelForm):
    class Meta:
        model=Camera
        fields=['name','ip','serial','location','manufacturer','model','api_url','enabled']
        labels={'name':'Nome','ip':'IP do dispositivo','serial':'Número de série','location':'Localização','manufacturer':'Fabricante','model':'Modelo','api_url':'API web do equipamento','enabled':'Ativo'}
    def clean_serial(self):return self.cleaned_data['serial'] or None
