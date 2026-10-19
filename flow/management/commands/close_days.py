import logging
from django.core.management.base import BaseCommand
class Command(BaseCommand):
    def handle(self,*a,**kw):
        from flow.operations import monitor_devices,generate_summaries
        from flow.reliability import process_recoveries
        from flow.whatsapp import scheduled_send
        from flow.notifications import pulse,runtime_failure,resolve
        pulse('closer')
        # Queue recovery before the message verifier inspects pending work.
        # Independent stages: verification still blocks unconfirmed totals,
        # but an unrelated recovery/alert failure must not stop scheduling.
        for task in (process_recoveries,monitor_devices,scheduled_send,generate_summaries):
            try:
                task();resolve('runtime:'+task.__name__)
            except Exception as error:
                runtime_failure(task.__name__,error)
                logging.getLogger(__name__).exception('Falha na etapa %s do ciclo de fechamento',task.__name__)

        pulse('closer')
