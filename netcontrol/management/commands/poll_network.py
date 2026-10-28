import os
from django.core.management.base import BaseCommand
from django.utils import timezone
from netcontrol.models import NetworkAsset, Interface, PollHistory
from netcontrol.snmp import collect_snmp

def poll_snmp_asset(asset):
    if not asset.ip or not asset.credential_env:
        raise ValueError('Configure IP e variável de ambiente da comunidade SNMP.')
    community = os.getenv(asset.credential_env)
    if not community:
        raise ValueError('Variável de ambiente da comunidade SNMP não disponível no coletor.')
    result = collect_snmp(asset.ip, community)
    for item in result['interfaces']:
        Interface.objects.update_or_create(
            asset=asset,
            index=item['index'],
            defaults={
                'name': item['name'],
                'status': item.get('status', 'unknown'),
                'speed_bps': item.get('speed_bps'),
                'mac': item.get('mac', ''),
            },
        )
    count = len(result['interfaces'])
    return f'{count} interfaces consultadas via SNMP'

class Command(BaseCommand):
    help='Consulta RouterOS REST e SNMP v2c em modo somente leitura.'
    def add_arguments(self, parser):
        parser.add_argument('--asset', type=int)
    def handle(self,*args,**options):
        import requests
        assets=NetworkAsset.objects.filter(enabled=True,protocol__in=('routeros','snmp'))
        if options['asset']:assets=assets.filter(pk=options['asset'])
        for a in assets:
            try:
                if a.protocol=='snmp':
                    detail=poll_snmp_asset(a)
                    a.last_status='online';a.last_error=''
                    success=True
                    a.last_checked=timezone.now();a.save(update_fields=['last_status','last_checked','last_error'])
                    PollHistory.objects.create(asset=a,successful=True,detail=detail)
                    self.stdout.write(f'{a.name}: {detail}')
                    continue
                if not a.ip or not a.username or not a.credential_env or not os.getenv(a.credential_env):
                    raise ValueError('Configure IP, usuário e variável de ambiente de credenciais')
                if not a.verify_tls: raise ValueError('TLS não verificado: configure certificado válido')
                url=f'https://{a.ip}/rest/interface'
                r=requests.get(url,auth=(a.username,os.environ[a.credential_env]),timeout=8,verify=True,headers={'Accept':'application/json'})
                r.raise_for_status()
                data=r.json()
                if not isinstance(data,list):raise ValueError('Resposta REST inesperada')
                for i,p in enumerate(data):
                    raw=p.get('.id','')
                    # A REST interface index may not be numeric; use persistent name for identity.
                    name=p.get('name',raw)
                    if not name:continue
                    obj=Interface.objects.filter(asset=a,name=name).first()
                    if obj is None:
                        used=set(Interface.objects.filter(asset=a).values_list('index',flat=True))
                        index=next(j for j in range(1,65536) if j not in used)
                        obj=Interface(asset=a,index=index,name=name)
                    obj.status='online' if p.get('running') in (True,'true','yes') else 'offline'
                    obj.mac=p.get('mac-address','') or ''
                    obj.save()
                a.last_status='online';a.last_error=''
                success=True;detail=f'{len(data)} interfaces consultadas'
            except Exception as e:
                a.last_status='error';a.last_error=str(e)[:240];success=False;detail=a.last_error
            a.last_checked=timezone.now();a.save(update_fields=['last_status','last_checked','last_error'])
            PollHistory.objects.create(asset=a,successful=success,detail=detail)
            self.stdout.write(f'{a.name}: {detail}')
