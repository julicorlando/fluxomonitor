from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help='Supervisiona a leitura de placas dos NVRs configurados no painel'
    def handle(self,*args,**options):
        from flow.plate_worker import supervise
        supervise()
