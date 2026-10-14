import os,time,logging
from django.core.management.base import BaseCommand,CommandError
from django.db import close_old_connections
from flow.models import Camera,PullCursor,RawDelivery
from flow.pfc_pull import poll,mirror

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
            sources={}
            for camera in sorted(cameras,key=lambda c:(c.replica_slot,c.pk)):
                close_old_connections()
                try:
                    if camera.ip in sources:
                        count=mirror(sources[camera.ip],camera)
                    else:
                        count=poll(camera)
                        camera.refresh_from_db(fields=["last_seen"])
                        sources[camera.ip]=camera
                    self.stdout.write(f'{camera.ip}: {count} eventos consultados',ending='\n')
                except Exception as error:
                    close_old_connections()
                    # Do not log URLs, signatures or credentials.
                    message=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__
                    PullCursor.objects.update_or_create(camera=camera,defaults={'last_error':message})
                    payload=getattr(error,'payload',None)
                    if payload is not None:
                        previous=RawDelivery.objects.filter(camera=camera).order_by('-id').first()
                        if not previous or previous.payload!=payload or previous.status!='pull_format_error':
                            RawDelivery.objects.create(camera=camera,payload=payload,status='pull_format_error')
                    logging.error('%s: %s',camera.ip,message)
            if options['once']:return
            time.sleep(max(0,interval-(time.monotonic()-started)))
