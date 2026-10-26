"""Read-only deployment smoke check. Never sends a WhatsApp message."""
import hashlib
import hmac
import json
from urllib.request import Request, urlopen
from django.core.management.base import BaseCommand, CommandError
from helpdesk.models import BotConfig, Company, Inbound, Outbound
from helpdesk.transport import hook_key


class Command(BaseCommand):
    help = 'Confere estrutura e webhook do suporte sem enviar mensagens ou criar chamados.'

    def handle(self, *args, **options):
        codes = set(Company.objects.values_list('code', flat=True))
        if codes != {'PDU', 'CM', 'SC', 'EVPI', 'PRED', 'FRT', 'TSP'}:
            raise CommandError('Empresas esperadas não foram encontradas.')
        before = (Inbound.objects.count(), Outbound.objects.count())
        # Unrecognized event, authenticated: validates the real HTTP route and HMAC only.
        raw = json.dumps({'session': 'default', 'event': 'support.health', 'payload': {}}).encode()
        signature = hmac.new(hook_key().encode(), raw, hashlib.sha512).hexdigest()
        request = Request('http://web:8000/support/webhook/', data=raw, method='POST', headers={
            'Host': 'localhost', 'Content-Type': 'application/json',
            'X-Webhook-Hmac': signature, 'X-Webhook-Hmac-Algorithm': 'sha512'})
        with urlopen(request, timeout=5) as response:
            if response.status != 204:
                raise CommandError('Webhook não confirmou o recebimento.')
        if before != (Inbound.objects.count(), Outbound.objects.count()):
            self.stdout.write('Há processamento concorrente; confira as filas no painel.')
        cfg = BotConfig.objects.get(pk=1)
        self.stdout.write(self.style.SUCCESS(f'SUPORTE OK: 7 empresas; webhook autenticado HTTP 204; bot={cfg.enabled}; hook_configurado={cfg.webhook_ready}; worker={bool(cfg.worker_at)}. Nenhum envio de teste foi solicitado.'))
