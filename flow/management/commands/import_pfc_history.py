import logging,time,os
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from flow.models import HistoryImport,RawDelivery
from flow.history_pull import import_batch

class Command(BaseCommand):
    help='Importa históricos solicitados no painel, em lotes, sem alterar o cursor ao vivo'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        ips=[v.strip() for v in os.environ.get('PFC_PULL_IPS','').split(',') if v.strip()]
        while True:
            close_old_connections()
            job=None
            try:
                job=HistoryImport.objects.filter(status__in=['pending','running'],camera__ip__in=ips).order_by('id').first()
                if job:import_batch(job.pk)
            except Exception as error:
                close_old_connections()
                if job:
                    message=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__
                    HistoryImport.objects.filter(pk=job.pk).update(status='failed',error=message)
                    if getattr(error,'payload',None) is not None:RawDelivery.objects.create(camera=job.camera,payload=error.payload,status='pull_format_error')
                    logging.error('Importação %s: %s',job.pk,message)
                else:logging.error('Aguardando banco de dados: %s',type(error).__name__)
            if options['once']:return
            time.sleep(2)
