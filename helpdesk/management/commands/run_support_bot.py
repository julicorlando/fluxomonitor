import logging
import time
from concurrent.futures import ThreadPoolExecutor
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections
from django.utils import timezone
from helpdesk.models import BotConfig, Inbound
from helpdesk.services import process_message
from helpdesk.bindings import recover_bound_pending
from helpdesk.transport import dispatch, ensure_webhook
from flow.notifications import runtime_failure, resolve
from helpdesk.alerts import sla_pass


def inbox_pass():
    recover_bound_pending()
    for pk in Inbound.objects.filter(processed_at__isnull=True, attempts__lt=5).order_by('id').values_list('id', flat=True)[:50]:
        try:
            process_message(pk)
        except Exception:
            from django.db.models import F
            Inbound.objects.filter(pk=pk).update(attempts=F('attempts') + 1, error='Falha no processamento. Verifique os logs do suporte.')
            raise


def safe_pass(name, operation):
    close_old_connections()
    try:
        operation()
        resolve('runtime:' + name)
    except Exception as exc:
        logging.exception('Falha em %s', name)
        runtime_failure(name, exc)
        if name == 'support_hook':
            BotConfig.objects.filter(pk=1).update(webhook_ready=False, detail='Não foi possível confirmar o recebimento. Verifique a conexão WhatsApp e os logs do serviço support.')
    finally:
        connections.close_all()


class Command(BaseCommand):
    help = 'Atendimento de TI usando a sessão WAHA existente, com filas persistentes.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO)
        logging.info('Suporte de TI iniciado: fila persistente e sessão WhatsApp default.')
        if options['once']:
            for name, operation in [('support_hook', ensure_webhook), ('support_inbox', inbox_pass), ('support_send', dispatch), ('support_sla', sla_pass)]:
                safe_pass(name, operation)
            BotConfig.objects.filter(pk=1).update(worker_at=timezone.now())
            return
        # Slow network requests never block receipt processing.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='support') as pool:
            jobs = {}
            last_hook = last_sla = 0
            while True:
                close_old_connections()
                BotConfig.objects.filter(pk=1).update(worker_at=timezone.now())
                for name, operation in [('support_inbox', inbox_pass), ('support_send', dispatch)]:
                    if name not in jobs or jobs[name].done():
                        jobs[name] = pool.submit(safe_pass, name, operation)
                if time.monotonic() - last_sla >= 30 and ('support_sla' not in jobs or jobs['support_sla'].done()):
                    jobs['support_sla'] = pool.submit(safe_pass, 'support_sla', sla_pass)
                    last_sla = time.monotonic()
                if time.monotonic() - last_hook >= 60 and ('support_hook' not in jobs or jobs['support_hook'].done()):
                    jobs['support_hook'] = pool.submit(safe_pass, 'support_hook', ensure_webhook)
                    last_hook = time.monotonic()
                time.sleep(1)
