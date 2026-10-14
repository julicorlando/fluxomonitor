import os,time,logging
from django.core.management.base import BaseCommand,CommandError
from django.db import close_old_connections
from flow.models import Camera,PullCursor
from flow.pfc_pull import poll

class Command(BaseCommand):
    help='Consulta eventos PFC com assinatura validada no firmware do equipamento'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        ips=[s.strip() for s in os.environ.get('PFC_PULL_IPS','').split(',') if s.strip()]
        if not ips:raise CommandError('Configure PFC_PULL_IPS com os IPs cadastrados')
        if not os.environ.get('PFC_SERVICE_SECRET'):raise CommandError('Configure PFC_SERVICE_SECRET')
        interval=max(5,int(os.environ.get('PFC_POLL_SECONDS','5')))
        while True:
            started=time.monotonic()
            cameras=list(Camera.objects.filter(enabled=True,ip__in=ips))
            missing=set(ips)-{c.ip for c in cameras}
            if missing:logging.error('IPs ausentes ou desativados: %s',','.join(sorted(missing)))
            for camera in cameras:
                close_old_connections()
                try:
                    count=poll(camera)
                    self.stdout.write(f'{camera.ip}: {count} eventos consultados',ending='\n')
                except Exception as error:
                    close_old_connections()
                    # Do not log URLs, signatures or credentials.
                    message=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__
                    PullCursor.objects.update_or_create(camera=camera,defaults={'last_error':message})
                    logging.error('%s: %s',camera.ip,message)
            if options['once']:return
            time.sleep(max(0,interval-(time.monotonic()-started)))
