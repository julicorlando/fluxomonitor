import os
from django.core.management.base import BaseCommand
from django.utils import timezone
from netcontrol.models import NetworkAsset, Interface, PollHistory

class Command(BaseCommand):
    help='Consulta dispositivos via RouterOS REST HTTPS somente leitura (sem alterações no equipamento).'
    def add_arguments(self, parser):
        parser.add_argument('--asset', type=int)
    def handle(self,*args,**options):
        import requests
        assets=NetworkAsset.objects.filter(enabled=True,protocol='routeros')
        if options['asset']:assets=assets.filter(pk=options['asset'])
        for a in assets:
            try:
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
