"""Bounded read-only network/API probe. Never advances the collection cursor."""
import ipaddress,os,socket,time
from datetime import timedelta
from urllib.error import HTTPError,URLError
from django.db import transaction
from django.contrib.auth.decorators import login_required,user_passes_test
from django.views.decorators.http import require_http_methods
from django.shortcuts import get_object_or_404,render
from django.utils import timezone
from .models import Camera,PullCursor,CommunicationCheck,CollectionGap,HistoryImport,ServicePulse
from .pfc_pull import fetch,ProtocolError
from .services import ZONE

PRIVATE=[ipaddress.ip_network(v) for v in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]

def probe(camera):
    steps=[];address=ipaddress.ip_address(camera.ip)
    if address.version!=4 or not any(address in net for net in PRIVATE):
        return {'state':'blocked','title':'Endereço fora da rede privada suportada','steps':[{'label':'Endereço','state':'warn','detail':'O teste atende apenas IPs IPv4 privados cadastrados. Não consulta URLs informadas manualmente.'}]}
    start=time.monotonic()
    try:
        with socket.create_connection((camera.ip,8020),timeout=3):pass
        steps.append({'label':'Rede e porta TCP 8020','state':'good','detail':f'Conexão estabelecida em {round((time.monotonic()-start)*1000)} ms.'})
    except (TimeoutError,socket.timeout):
        steps.append({'label':'Rede e porta TCP 8020','state':'bad','detail':'Tempo esgotado. Possíveis causas: equipamento desligado, rota/VLAN ou bloqueio no firewall. O teste não identifica sozinho qual delas ocorreu.'})
        return {'state':'offline','title':'O serviço do equipamento não respondeu','steps':steps}
    except OSError as error:
        detail='Conexão recusada: o IP respondeu, mas o serviço na porta 8020 não aceitou a conexão.' if isinstance(error,ConnectionRefusedError) else 'Não foi possível abrir a conexão. Confira IP, rota/VLAN, firewall e serviço do equipamento.'
        steps.append({'label':'Rede e porta TCP 8020','state':'bad','detail':detail})
        return {'state':'offline','title':'Falha de acesso à porta do equipamento','steps':steps}
    if not os.getenv('PFC_SERVICE_SECRET'):
        steps.append({'label':'API autenticada','state':'bad','detail':'Chave de integração não configurada no servidor. Configure PFC_SERVICE_SECRET no Coolify.'})
        return {'state':'configuration','title':'Rede acessível; integração sem chave','steps':steps}
    source=Camera.objects.filter(ip=camera.ip,enabled=True).order_by('replica_slot','id').first() or camera
    cursor=PullCursor.objects.filter(camera=source).first()
    start=time.monotonic()
    try:
        fetch(source,cursor.offset if cursor and cursor.initialized else 0,1,timeout=4)
        steps.append({'label':'API autenticada e formato dos dados','state':'good','detail':f'Consulta assinada aceita e resposta validada em {round((time.monotonic()-start)*1000)} ms. Nenhuma contagem foi alterada.'})
        return {'state':'online','title':'O equipamento está respondendo à API','steps':steps}
    except HTTPError as error:
        detail=f'API respondeu HTTP {error.code}. '+('Verifique a autenticação do equipamento.' if error.code in (401,403) else 'Confira se o serviço e o caminho da API correspondem a este firmware.')
    except ProtocolError as error:
        message=str(error).lower()
        detail='Assinatura/chave recusada pelo equipamento. Confira a chave de integração configurada no servidor.' if any(v in message for v in ('sign','token','auth','senha','password')) else 'O equipamento respondeu, mas recusou a consulta ou enviou dados fora do formato esperado. Consulte o diagnóstico e a versão do firmware.'
    except (TimeoutError,socket.timeout,URLError):detail='A porta abriu, mas a consulta à API não terminou. O equipamento pode estar ocupado ou haver instabilidade na rede.'
    except (ValueError,UnicodeError):detail='A porta abriu, mas a resposta não é um JSON válido para a integração.'
    except OSError:detail='Conexão interrompida durante a consulta da API. Verifique a rede e o serviço do equipamento.'
    steps.append({'label':'API autenticada e formato dos dados','state':'bad','detail':detail})
    return {'state':'api_error','title':'Porta acessível; falha na consulta da API','steps':steps}

@login_required
@user_passes_test(lambda u:u.is_staff)
@require_http_methods(['GET','POST'])
def communication(request,device_id):
    camera=get_object_or_404(Camera,pk=device_id);now=timezone.now();reused=False
    check=CommunicationCheck.objects.filter(ip=camera.ip).first()
    if request.method=='POST':
        with transaction.atomic():
            check,_=CommunicationCheck.objects.get_or_create(ip=camera.ip)
            check=CommunicationCheck.objects.select_for_update().get(pk=check.pk)
            # A first row has no result; claim it without locking the camera itself.
            recent=(check.result or check.finished_at is None and check.result.get('running')) and now-check.started_at<timedelta(seconds=15)
            if recent:reused=True
            else:
                check.started_at=now;check.finished_at=None;check.result={'running':True};check.save()
        if not reused:
            try:result=probe(camera)
            except Exception:result={'state':'error','title':'Não foi possível concluir o diagnóstico','steps':[{'label':'Servidor','state':'bad','detail':'Falha interna durante o teste. Consulte os logs; nenhuma contagem foi alterada.'}]}
            check.result=result;check.finished_at=timezone.now();check.save(update_fields=['result','finished_at'])
    source=Camera.objects.filter(ip=camera.ip,enabled=True).order_by('replica_slot','id').first() or camera
    cursor=PullCursor.objects.filter(camera=source).first();beat=ServicePulse.objects.filter(name='collector').first()
    today=now.astimezone(ZONE).date()
    gaps=CollectionGap.objects.filter(camera__ip=camera.ip).exclude(state='recovered').count()
    jobs=list(HistoryImport.objects.filter(camera__ip=camera.ip,status__in=['pending','running','paused','failed'],start__lte=today,end__gte=today).order_by('-pk')[:5])
    configured=camera.enabled and camera.ip in [s.strip() for s in os.getenv('PFC_PULL_IPS','').split(',')]
    data={'camera':camera,'check':check,'reused':reused,'cursor':cursor,'gaps':gaps,'jobs':jobs,'configured':configured,'collector_active':bool(beat and now-beat.checked_at<timedelta(minutes=5)),'poll_seconds':max(2,int(os.getenv('PFC_POLL_SECONDS','3'))),'source':source}
    response=render(request,'device_communication.html',data);response['Cache-Control']='private, no-store';return response
