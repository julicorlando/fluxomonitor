from django.core.management.base import BaseCommand
from flow.models import Camera
class Command(BaseCommand):
    def handle(self,*a,**kw):
        for i,n in enumerate((235,236,237),1):
            if not Camera.objects.filter(ip=f'192.168.1.{n}').exists():
                Camera.objects.create(ip=f'192.168.1.{n}',name=f'Dispositivo {i}')
