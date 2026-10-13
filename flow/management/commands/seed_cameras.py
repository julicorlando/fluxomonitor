from django.core.management.base import BaseCommand
from flow.models import Camera
class Command(BaseCommand):
    def handle(self,*a,**kw):
        for i,n in enumerate((235,236,237),1):Camera.objects.get_or_create(ip=f'192.168.1.{n}',defaults={'name':f'Câmera {i}'})
