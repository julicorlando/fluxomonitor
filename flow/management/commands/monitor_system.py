import logging,time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen,Request
from django.core.management.base import BaseCommand
from django.db import close_old_connections,connections
from flow.notifications import scan_system,dispatch_notices,notice,resolve,pulse,runtime_failure
from flow.reliability import process_recoveries


def recovery_pass():
    close_old_connections()
    try:process_recoveries();resolve('runtime:process_recoveries')
    except Exception as error:
        logging.exception('Falha na recuperação independente');runtime_failure('process_recoveries',error)
    finally:connections.close_all()

class Command(BaseCommand):
    help='Monitor independente de falhas e horários de envio; avisos persistidos no banco'
    def add_arguments(self,parser):parser.add_argument('--once',action='store_true')
    def handle(self,*args,**options):
        # Recovery can wait for camera locks/network IO. Keep it off the alert loop.
        with ThreadPoolExecutor(max_workers=1,thread_name_prefix='recovery') as pool:
            recovery=None
            while True:
                close_old_connections()
                if options['once']:recovery_pass()
                elif recovery is None or recovery.done():recovery=pool.submit(recovery_pass)
                try:scan_system();resolve('runtime:scan_system')
                except Exception as error:
                    logging.exception('Falha no monitor de ocorrências');runtime_failure('scan_system',error)
                try:
                    with urlopen(Request('http://web:8000/health/',headers={'Host':'localhost'}),timeout=5) as response:
                        if response.status!=200:raise RuntimeError('health')
                    resolve('web-health')
                except Exception:notice('web-health','Aplicação web indisponível','O monitor não conseguiu consultar a saúde da aplicação web. Verifique o serviço web e a rede interna.')
                try:dispatch_notices();pulse('notifier')
                except Exception:logging.exception('Falha ao processar avisos; aguardando próximo ciclo')
                if options['once']:return
                time.sleep(30)
