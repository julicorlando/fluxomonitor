from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help='Processo interno de leitura de um canal de NVR'
    def add_arguments(self,parser): parser.add_argument('camera_id',type=int)
    def handle(self,*args,**options):
        from flow.plate_worker import read_channel
        read_channel(options['camera_id'])
