import json
from django.core.management.base import BaseCommand
from flow.import_cleanup import preview,cleanup,restore
class Command(BaseCommand):
    help='Limpa apenas importações com cópia recuperável; sem --apply apenas informa quantidades'
    def add_arguments(self,parser):
        parser.add_argument('--apply',action='store_true')
        parser.add_argument('--restore',type=int)
    def handle(self,*a,**options):
        if options['restore']:
            restore(options['restore'],'management');self.stdout.write('Restauração concluída');return
        if not options['apply']:self.stdout.write(json.dumps(preview()));return
        archive=cleanup('management')
        self.stdout.write(json.dumps({'archive_id':archive.pk if archive else None,'remaining':preview()}))
