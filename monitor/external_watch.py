"""Run outside the monitored server. Availability only; never retrieves counts."""
import json,os,time
from datetime import datetime,timezone
from urllib.request import Request,urlopen
from urllib.error import HTTPError

URL='https://fluxo.axionwebdigital.com.br/health/monitor/'
TITLE='[GesTi] Monitor externo: falha de disponibilidade'
MARKER='<!-- gesti-external-monitor-v1 -->'


def probe():
    try:
        with urlopen(Request(URL,headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'GesTi-ExternalMonitor/1'}),timeout=15) as response:
            data=json.loads(response.read(4096))
            observed=datetime.fromisoformat(data['observed_at'])
            fresh=observed.tzinfo is not None and -60<=(datetime.now(timezone.utc)-observed).total_seconds()<180
            return (response.status==200 and data.get('status')=='ok' and fresh),'Saúde validada' if fresh else 'Confirmação de saúde desatualizada'
    except HTTPError as error:return False,f'Endpoint externo respondeu HTTP {error.code}'
    except Exception as error:return False,f'Consulta HTTPS não concluída ({type(error).__name__})'


def github(method,path,body=None):
    token=os.environ['GH_TOKEN'];repo=os.environ['GITHUB_REPOSITORY']
    req=Request('https://api.github.com/repos/'+repo+path,method=method,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'})
    with urlopen(req,timeout=20) as response:return json.loads(response.read())


def report(healthy,reason):
    # A single bot-owned incident remains open until the service normalizes.
    issues=github('GET','/issues?state=open&creator=github-actions%5Bbot%5D&per_page=100')
    current=next((i for i in issues if i.get('title')==TITLE and MARKER in (i.get('body') or '')),None)
    stamp=datetime.now(timezone.utc).isoformat()
    if not healthy and not current:
        owner=os.environ['GITHUB_REPOSITORY'].split('/')[0]
        github('POST','/issues',{'title':TITLE,'body':f'{MARKER}\nVerificação externa falhou em três tentativas.\n\nURL pública: {URL}\nMotivo: {reason}\nInstante UTC: {stamp}\n\nO teste cobre aplicação, atividade dos serviços e sessão do WhatsApp, sem ler contagens. Bloqueios do provedor ou do caminho de rede também podem causar falha.','assignees':[owner]})
    elif healthy and current:
        github('POST',f'/issues/{current["number"]}/comments',{'body':f'Serviço normalizado na verificação externa. UTC: {stamp}. Nenhuma contagem foi consultada.'})
        github('PATCH',f'/issues/{current["number"]}',{'state':'closed','state_reason':'completed'})


def main():
    healthy=False;reason='Sem confirmação'
    for attempt in range(3):
        healthy,reason=probe()
        if healthy:break
        if attempt<2:time.sleep(5)
    report(healthy,reason)
    print('Saúde externa OK' if healthy else reason)
    return 0 if healthy else 1

if __name__=='__main__':raise SystemExit(main())
