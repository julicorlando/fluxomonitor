from django.core.management.base import BaseCommand
class Command(BaseCommand):
    def handle(self,*a,**kw):
        from flow.operations import monitor_devices,generate_summaries
        from flow.reliability import process_recoveries
        from flow.whatsapp import scheduled_send
        # Queue recovery before the message verifier inspects pending work.
        process_recoveries()
        monitor_devices()
        scheduled_send()
        generate_summaries()
