import secrets,hashlib
from django.core.management.base import BaseCommand
from flow.models import ApiKey,Camera
class Command(BaseCommand):
    def add_arguments(self,p):
        p.add_argument('name');p.add_argument('--camera',type=int);p.add_argument('--ingest',action='store_true');p.add_argument('--no-read',action='store_true')
    def handle(self,*args,**o):
        c=Camera.objects.get(pk=o['camera']) if o['camera'] else None
        token=secrets.token_urlsafe(32)
        ApiKey.objects.create(name=o['name'],digest=hashlib.sha256(token.encode()).hexdigest(),camera=c,can_ingest=o['ingest'],can_read=not o['no_read'])
        self.stdout.write(token)
