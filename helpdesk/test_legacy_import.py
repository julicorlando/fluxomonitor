import csv
import tempfile
from pathlib import Path
from django.core.management import call_command, CommandError
from django.test import TestCase
from helpdesk.models import Asset, Company


class LegacyInventoryImportTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'patrimonios.csv'
        fields = ['id','tomb_number','nf_number','nfe_date','supplier','item_name','brand','model',
                  'serial_number','imei','asset_state','asset_status','notes','department_name','employee_name']
        with self.path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for i in range(1,211):
                writer.writerow({'id':str(i), 'tomb_number':f'T-{i:04d}', 'nf_number':'NF-2026',
                    'nfe_date':'2026-10-20','supplier':'Teste','item_name':'Notebook',
                    'asset_state':'Novo','asset_status':'Ativo',
                    'department_name': 'ESCRITÓRIO PDU' if i<=205 else ''})

    def test_unassigned_rows_abort_all_writes(self):
        with self.assertRaises(CommandError):
            call_command('import_legacy_assets',str(self.path),'--apply')
        self.assertEqual(Asset.objects.count(),0)

    def test_all_210_import_once_and_remain_idempotent(self):
        args=[f'--assign={i}=PDU' for i in range(206,211)]
        call_command('import_legacy_assets',str(self.path),*args,'--dry-run')
        self.assertEqual(Asset.objects.count(),0)
        call_command('import_legacy_assets',str(self.path),*args,'--apply')
        self.assertEqual(Asset.objects.count(),210)
        self.assertEqual(Asset.objects.first().invoice_number,'NF-2026')
        call_command('import_legacy_assets',str(self.path),*args,'--apply')
        self.assertEqual(Asset.objects.count(),210)
