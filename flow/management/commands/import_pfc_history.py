import logging,time,os
from concurrent.futures import ThreadPoolExecutor
from django.core.management.base import BaseCommand
from django.db import close_old_connections,connections
from django.db.models import Case,When,Value,IntegerField
from django.utils import timezone
from flow.services import ZONE
from flow.models import HistoryImport,RawDelivery
from flow.history_pull import import_batch
from flow.notifications import pulse,runtime_failure,resolve


def next_job(ip):
    today=timezone.now().astimezone(ZONE).date()
    return HistoryImport.objects.filter(status__in=['pending','running'],camera__ip=ip).annotate(priority=Case(When(start__lte=today,end__gte=today,then=Value(0)),default=Value(1),output_field=IntegerField())).order_by('priority','id').first()


def run_batch(ip):
    close_old_connections()
    job=None
    try:
        job=next_job(ip)
        if job:import_batch(job.pk)
        resolve('runtime:history')
    except Exception as error:
        close_old_connections()
        if job:
            message=str(error)[:250] if isinstance(error,ValueError) else type(error).__name__
            # Never turn an already cancelled/archived job into a failed one.
            HistoryImport.objects.filter(pk=job.pk,status__in=['pending','running']).update(status='failed',error=message,updated_at=timezone.now())
            if getattr(error,'payload',None) is not None:RawDelivery.objects.create(camera=job.camera,payload=error.payload,status='pull_format_error')
            logging.error('Importação %s: %s',job.pk,message)
        else:
            logging.error('Aguardando banco de dados (%s): %s',ip,type(error).__name__)
            runtime_failure('history',error)
    finally:
        connections.close_all()


class Command(BaseCommand):
    help='Importa em paralelo por equipamento físico, mantendo uma tarefa por IP'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        ips=list(dict.fromkeys(v.strip() for v in os.environ.get('PFC_PULL_IPS','').split(',') if v.strip()))
        if options['once']:
            # A maintenance pass uses this thread/connection, also suitable for
            # transactional management-command callers.
            for ip in ips:
                job=next_job(ip)
                if job:import_batch(job.pk)
            return
        pause=max(0.5,min(30,float(os.getenv('PFC_HISTORY_PAUSE_SECONDS','1'))))
        last_pulse=0
        with ThreadPoolExecutor(max_workers=max(1,len(ips)),thread_name_prefix='history') as pool:
            running={};ready={ip:0 for ip in ips}
            while True:
                now=time.monotonic()
                if now-last_pulse>=30:
                    pulse('history');last_pulse=now
                for ip in ips:
                    future=running.get(ip)
                    if future is not None:
                        if not future.done():continue
                        try:future.result()
                        except Exception:logging.exception('Falha no trabalhador de histórico %s',ip)
                        del running[ip];ready[ip]=now+pause
                    if now>=ready[ip]:running[ip]=pool.submit(run_batch,ip)
                time.sleep(0.25)
