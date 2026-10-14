from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from flow.models import Reading
from flow.services import close_day,ZONE
class Command(BaseCommand):
    def handle(self,*a,**kw):
        from flow.operations import monitor_devices,generate_summaries
        from flow.whatsapp import scheduled_send
        scheduled_send()
        monitor_devices()
        generate_summaries()
        first=Reading.objects.order_by('timestamp').first()
        if not first:return
        day=first.timestamp.astimezone(ZONE).date();today=timezone.localdate()
        while day<today:close_day(day);day+=timedelta(days=1)
