import os,time,logging
from concurrent.futures import ThreadPoolExecutor
from django.core.management.base import BaseCommand,CommandError
from django.db import close_old_connections,connections
from flow.models import Camera,PullCursor,RawDelivery
from flow.pfc_pull import poll,mirror
from flow.reliability import observe_collection,source_for,process_recoveries
from flow.notifications import pulse,runtime_failure,resolve


def collect_group(cameras):
    """One physical source followed by its replicas, never simultaneous per IP."""
    sources={};resumed=False;success=True
    for camera in sorted(cameras,key=lambda c:(c.replica_slot,c.pk)):
        close_old_connections()
        previous_success=PullCursor.objects.filter(camera=camera).values_list('last_success',flat=True).first()
        try:
            if camera.ip in sources:
                count=mirror(sources[camera.ip],camera)
            else:
                if source_for(camera).pk!=camera.pk:raise ValueError('Equipamento original indisponível; réplica aguarda sincronização')
                count=poll(camera)
                resumed=observe_collection(camera,True,previous_success) or resumed
                camera.refresh_from_db(fields=['last_seen']);sources[camera.ip]=camera
            logging.info('%s: %s eventos consultados',camera.ip,count)
        except Exception as error:
            success=False;close_old_connections()
            message=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__
            PullCursor.objects.update_or_create(camera=camera,defaults={'last_error':message})
            observe_collection(camera,False,previous_success)
            payload=getattr(error,'payload',None)
            if payload is not None:
                previous=RawDelivery.objects.filter(camera=camera).order_by('-id').first()
                if not previous or previous.payload!=payload or previous.status!='pull_format_error':
                    RawDelivery.objects.create(camera=camera,payload=payload,status='pull_format_error')
            logging.error('%s: %s',camera.ip,message)
            # A replica cannot poll the same failed hardware as another source.
            break
    return {'success':success,'resumed':bool(resumed)}


def collect_ip(ip):
    close_old_connections()
    try:
        cameras=list(Camera.objects.filter(enabled=True,ip=ip))
        if not cameras:
            runtime_failure('collector-config',ValueError('Dispositivo ausente'))
            return {'success':False,'resumed':False}
        return collect_group(cameras)
    finally:connections.close_all()


def recover():
    close_old_connections()
    try:process_recoveries();resolve('runtime:collector-recovery')
    except Exception as error:
        runtime_failure('collector-recovery',error);logging.exception('Falha ao iniciar recuperação após reconexão')
    finally:connections.close_all()


def dispatch_ready(pool,running,ready,ips,now,interval,started):
    """Observe only completed futures so a slow IP cannot hold the others."""
    resumed=False
    for ip in ips:
        future=running.get(ip)
        if future is not None:
            if not future.done():continue
            try:
                result=future.result();resumed=resumed or result['resumed']
                ready[ip]=max(now if result['success'] else now+5,started[ip]+interval)
            except Exception as error:
                runtime_failure('collector',error);logging.exception('Falha no trabalhador de coleta %s',ip);ready[ip]=now+5
            del running[ip]
        if now>=ready[ip]:
            started[ip]=now;running[ip]=pool.submit(collect_ip,ip)
    return resumed


class Command(BaseCommand):
    help='Consulta em paralelo por IP físico, preservando cursor e réplicas'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        ips=list(dict.fromkeys(s.strip() for s in os.environ.get('PFC_PULL_IPS','').split(',') if s.strip()))
        if not ips:raise CommandError('Configure PFC_PULL_IPS com os IPs cadastrados')
        if not os.environ.get('PFC_SERVICE_SECRET'):raise CommandError('Configure PFC_SERVICE_SECRET')
        interval=max(2,int(os.environ.get('PFC_POLL_SECONDS','3')))
        if options['once']:
            pulse('collector');resumed=False
            for ip in ips:
                cameras=list(Camera.objects.filter(enabled=True,ip=ip))
                if cameras:resumed=collect_group(cameras)['resumed'] or resumed
            if resumed:
                try:process_recoveries();resolve('runtime:collector-recovery')
                except Exception as error:runtime_failure('collector-recovery',error);logging.exception('Falha ao iniciar recuperação após reconexão')
            pulse('collector');return
        with ThreadPoolExecutor(max_workers=min(8,len(ips))+1,thread_name_prefix='collector') as pool:
            running={};ready={ip:0 for ip in ips};started={};recovery=None;last_pulse=0
            while True:
                now=time.monotonic()
                if now-last_pulse>=30:
                    close_old_connections();pulse('collector');last_pulse=now
                resumed=dispatch_ready(pool,running,ready,ips,now,interval,started)
                if resumed and (recovery is None or recovery.done()):recovery=pool.submit(recover)
                time.sleep(0.1)
