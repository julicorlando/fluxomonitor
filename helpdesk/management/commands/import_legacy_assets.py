"""Importa somente patrimônio legado exportado, sem alterar cadastros existentes.

Uso:
 python manage.py import_legacy_assets /app/imports/gesti_patrimonios_210_importacao.csv --dry-run
 python manage.py import_legacy_assets /app/imports/gesti_patrimonios_210_importacao.csv --assign 13=PDU --assign 14=PDU --assign 37=PDU --assign 38=PDU --assign 176=PDU --apply

O operador deve escolher individualmente a empresa dos registros sem departamento.
"""
import csv
from datetime import date
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from helpdesk.models import Asset, Company, Contact

DEPARTMENT_COMPANY = {
    'Obra FRT':'FRT',
    'ALMOXERIFADO SCQ':'PDU',
    'ESCRITÓRIO PDU':'PDU',
    'EV PDU':'EVPI',
    'Obra TSP':'TSP',
    'Obra SCQ':'PDU',
    'SHOPPING CARPINA':'SC',
    'CARPINA MALL':'CM',
}
EXPECTED_COLUMNS = {
    'id','tomb_number','nf_number','nfe_date','supplier','item_name',
    'brand','model','serial_number','imei','asset_state','asset_status',
    'notes','department_name','employee_name',
}


class Command(BaseCommand):
    help = 'Importa inventário legado CSV sem substituir dados existentes; revisão explícita de empresas ausentes.'

    def add_arguments(self, parser):
        parser.add_argument('file')
        parser.add_argument('--assign', action='append', default=[], metavar='ID=EMPRESA')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        if opts['apply'] and opts['dry_run']:
            raise CommandError('Use --apply ou --dry-run, nunca ambos.')
        assignments = {}
        official = {c[0] for c in Company._meta.get_field('code').choices}
        for raw in opts['assign']:
            try:
                source_id, code = raw.split('=', 1)
                if not source_id.isdecimal() or code not in official:
                    raise ValueError()
                assignments[source_id] = code
            except ValueError:
                raise CommandError('Informe --assign ID=CODIGO (empresa cadastrada).')
        filename = Path(opts['file'])
        if not filename.is_file() or filename.stat().st_size > 5_000_000:
            raise CommandError('Informe arquivo CSV existente de até 5 MB.')
        with filename.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not EXPECTED_COLUMNS.issubset(set(reader.fieldnames or [])):
                raise CommandError('CSV sem as colunas de inventário esperadas.')
            rows = list(reader)
        if len(rows) != 210:
            raise CommandError(f'Esperados 210 patrimônios; encontrados {len(rows)}.')
        ids = [row['id'] for row in rows]
        if len(ids) != len(set(ids)):
            raise CommandError('IDs legados repetidos no arquivo.')
        unassigned = []
        validated = []
        for row in rows:
            code = DEPARTMENT_COMPANY.get(row['department_name'].strip())
            if not code:
                code = assignments.get(row['id'])
            if not code:
                unassigned.append((row['id'],row['tomb_number']))
            if not row['tomb_number'].strip() or not row['nf_number'].strip() or not row['nfe_date'].strip():
                raise CommandError('Patrimônio, NF e data ausentes no legado ID '+row['id'])
            try:
                acquired = date.fromisoformat(row['nfe_date'].strip())
            except ValueError as exc:
                raise CommandError('Data inválida ID '+row['id']) from exc
            validated.append((row, code, acquired))
        if unassigned:
            self.stdout.write('Sem empresa definida: '+', '.join(f'ID {pk} ({tag})' for pk,tag in unassigned))
            raise CommandError('Defina cada empresa com --assign ID=CODIGO; nenhum patrimônio foi importado.')
        missing_companies = sorted({code for _,code,_ in validated if not Company.objects.filter(code=code).exists()})
        if missing_companies:
            raise CommandError('Empresas ausentes no GesTI: '+', '.join(missing_companies))
        keys = [(code, row['tomb_number'].strip()) for row,code,_ in validated]
        if len(keys) != len(set(keys)):
            raise CommandError('Patrimônio duplicado dentro da mesma empresa no CSV.')
        counts = {'created':0,'already_present':0}
        with transaction.atomic():
            for row,code,acquired in validated:
                company = Company.objects.get(code=code)
                tag = row['tomb_number'].strip()
                if Asset.objects.filter(company=company,tag=tag).exists():
                    counts['already_present'] += 1
                    continue
                if opts['dry_run'] or not opts['apply']:
                    counts['created'] += 1
                    continue
                item_name = row['item_name'].strip()
                lower = item_name.lower()
                kind = ('computer' if any(w in lower for w in ('notebook','computador','desktop','pc')) else
                        'printer' if 'impressora' in lower else 'network' if any(w in lower for w in ('roteador','switch','access point')) else
                        'server' if 'servidor' in lower else 'other')
                status = row['asset_status'].strip().lower()
                state = 'active' if status == 'ativo' else 'maintenance' if status == 'defeituoso' else 'retired'
                employee = row['employee_name'].strip()
                contact = Contact.objects.filter(company=company,name__iexact=employee,active=True).first() if employee else None
                notes = '\n'.join(p for p in [
                    row['notes'].strip(), 'Importado do GesTI legado; ID original: '+row['id'],
                    'Fornecedor: '+row['supplier'].strip() if row['supplier'].strip() else '',
                    'Marca: '+row['brand'].strip() if row['brand'].strip() else '',
                    'Modelo: '+row['model'].strip() if row['model'].strip() else '',
                    'IMEI: '+row['imei'].strip() if row['imei'].strip() else '',
                    'Condição: '+row['asset_state'].strip() if row['asset_state'].strip() else '',
                    'Estado legado: '+row['asset_status'].strip(),
                    'Responsável legado: '+employee if employee and not contact else '',
                    'Departamento legado: '+row['department_name'].strip() if row['department_name'].strip() else 'Departamento legado não informado — empresa atribuída na importação',
                ] if p)
                Asset.objects.create(company=company,tag=tag,name=(item_name or row['model'] or 'Equipamento')[:150],
                    kind=kind,state=state,contact=contact,serial=row['serial_number'].strip()[:120],
                    invoice_number=row['nf_number'].strip()[:120],acquisition_date=acquired,
                    location=row['department_name'].strip()[:200],notes=notes[:4000])
                counts['created'] += 1
            if not opts['apply']:
                transaction.set_rollback(True)
        label = 'APLICADO' if opts['apply'] else 'SIMULAÇÃO, sem gravação'
        self.stdout.write(self.style.SUCCESS(
            f'{label}: {counts["created"]} novos, {counts["already_present"]} já existentes; total {len(rows)}.'))
