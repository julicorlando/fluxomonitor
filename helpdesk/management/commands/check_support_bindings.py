from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from helpdesk.models import SenderIdentity, UnresolvedInbound
from helpdesk.transport import resolve_phone


class Command(BaseCommand):
    help = 'Verifica vínculos persistentes sem enviar mensagens nem criar chamados.'

    def add_arguments(self, parser):
        parser.add_argument('--contact', type=int)

    def handle(self, *args, **options):
        bindings = SenderIdentity.objects.filter(contact__active=True, phone=F('contact__phone'))
        if options['contact']:
            bindings = bindings.filter(contact_id=options['contact'])
        checked = 0
        for binding in bindings.order_by('pk'):
            if resolve_phone({'from': binding.chat_id}) != binding.phone:
                raise CommandError('Um vínculo persistente não reconheceu o remetente esperado.')
            checked += 1
        if options['contact'] and checked == 0:
            raise CommandError('O colaborador solicitado não possui vínculo persistente válido.')
        pending = UnresolvedInbound.objects.filter(status='unresolved', sender__in=bindings.values_list('chat_id', flat=True)).count()
        self.stdout.write(self.style.SUCCESS(f'VINCULOS OK: {checked} remetente(s) reconhecido(s) pelo vínculo local; {pending} pendência(s) para recuperação. Nenhum envio solicitado.'))
