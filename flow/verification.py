"""Fail-closed verification of a point-in-time PFC total before WhatsApp."""
import hashlib,json,os,time
from datetime import timedelta
from django.db import transaction,connection
from django.utils import timezone
from .models import Camera,HistoryImport,PullCursor,Reading,AuditLog,ImportCleanup,CollectionGap
from .pfc_pull import poll,mirror
from .services import report,bounds,ZONE

class VerificationError(ValueError):pass

def verified_total(day,*,lock_timeout_ms=None):
    started=time.monotonic()
    configured={ip.strip() for ip in os.getenv('PFC_PULL_IPS','').split(',') if ip.strip()}
    if not configured or not os.getenv('PFC_SERVICE_SECRET'):
        raise VerificationError('Consulta direta dos equipamentos não configurada')
    with transaction.atomic():
        if lock_timeout_ms is not None and connection.vendor=='postgresql':
            with connection.cursor() as cursor:cursor.execute("SET LOCAL lock_timeout = '3000ms'")
        devices=list(Camera.objects.select_for_update().filter(enabled=True).order_by('ip','replica_slot','id'))
        included=[c for c in devices if c.include_in_total]
        if not included:raise VerificationError('Nenhum dispositivo incluído na soma')
        if configured-{c.ip for c in devices}:raise VerificationError('Há equipamento configurado ausente ou desativado')
        if any(c.ip not in configured for c in included):raise VerificationError('Há dispositivo incluído sem consulta PFC verificável')
        relevant=[c for c in devices if c.ip in {i.ip for i in included}]
        if CollectionGap.objects.filter(camera__ip__in=[c.ip for c in relevant],started_at__lt=bounds(day)[1]).exclude(state='recovered').filter(ended_at__isnull=True).exists() or CollectionGap.objects.filter(camera__ip__in=[c.ip for c in relevant],started_at__lt=bounds(day)[1],ended_at__gte=bounds(day)[0]).exclude(state='recovered').exists():
            raise VerificationError('Falha de comunicação deste dia ainda sem recuperação concluída')
        if HistoryImport.objects.filter(camera__in=relevant,start__lte=day,end__gte=day).exclude(status__in=['completed','archived']).exists():
            raise VerificationError('Histórico deste dia pendente, interrompido ou com falha')
        for archive in ImportCleanup.objects.filter(restored_at__isnull=True).only('affected_days','created_at'):
            if str(day) in archive.affected_days:
                affected=archive.affected_days[str(day)] if isinstance(archive.affected_days,dict) else [c.pk for c in relevant]
                by_ip={}
                for c in relevant:by_ip.setdefault(c.ip,c)
                required={by_ip[c.ip].pk for c in relevant if c.pk in affected}
                for source_id in required:
                    if not HistoryImport.objects.filter(camera_id=source_id,start__lte=day,end__gte=day,status='completed',created_at__gt=archive.created_at).exists():
                        raise VerificationError('Dia com dados importados limpos; aguarde uma nova importação completa')
        sources={}
        for camera in relevant:
            if camera.ip in sources:
                mirror(sources[camera.ip],camera)
                continue
            state=PullCursor.objects.filter(camera=camera,initialized=True).first()
            if not state:raise VerificationError(f'{camera.name}: coleta ainda não inicializada')
            empty=0
            for attempt in range(20):
                if time.monotonic()-started>20:raise VerificationError('Verificação excedeu o tempo; nova tentativa no próximo ciclo')
                try:count=poll(camera)
                except Exception as exc:raise VerificationError(f'{camera.name}: consulta não confirmada') from exc
                empty=empty+1 if count==0 else 0
                if empty>=2:break
            if empty<2:raise VerificationError(f'{camera.name}: há eventos aguardando coleta')
            camera.refresh_from_db()
            state.refresh_from_db()
            if state.last_error or not state.last_success or timezone.now()-state.last_success>timedelta(seconds=60):
                raise VerificationError(f'{camera.name}: consulta sem confirmação recente')
            sources[camera.ip]=camera
        cutoff=timezone.now()
        if cutoff.astimezone(ZONE).date()!=day:raise VerificationError('O dia mudou durante a verificação')
        start,end=bounds(day)
        from .intelligence import closing_at
        reporting_cutoff=min(cutoff,closing_at(day))
        snapshots=[];source_rows={};entries=0
        # Check every event, including excluded physical sources of included replicas.
        for camera in relevant:
            if Reading.objects.filter(camera=camera,timestamp__gt=cutoff,timestamp__lt=end).exists():
                raise VerificationError(f'{camera.name}: registros com horário futuro')
            rows=list(Reading.objects.filter(camera=camera,timestamp__gte=start,timestamp__lte=reporting_cutoff).order_by('event_id').values_list('event_id','timestamp','entries','exits','mode'))
            if any(not eid.startswith('pfc-event:') or mode!='delta' or (a,b) not in ((1,0),(0,1)) for eid,stamp,a,b,mode in rows):
                raise VerificationError(f'{camera.name}: origem ou valor de evento inconsistente')
            if camera.ip in source_rows and rows!=source_rows[camera.ip]:
                raise VerificationError(f'{camera.name}: réplica difere do equipamento original')
            source_rows[camera.ip]=rows
            if camera.include_in_total:
                count=sum(row[2] for row in rows);entries+=count
                digest=hashlib.sha256(json.dumps(rows,default=str,separators=(',',':')).encode()).hexdigest()
                snapshots.append({'device_id':camera.pk,'replica_slot':camera.replica_slot,'events':len(rows),'entries':count,'digest':digest})
        first=report(day,until=reporting_cutoff+timedelta(microseconds=1));second=report(day,until=reporting_cutoff+timedelta(microseconds=1))
        expected={item['device_id']:item['entries'] for item in snapshots}
        for data in (first,second):
            actual={c['id']:c['entries'] for c in data['cameras'] if c['include_in_total']}
            if actual!=expected or data['totals']['entries']!=entries:
                raise VerificationError('A soma independente divergiu do painel')
        AuditLog.objects.create(actor='system',action='whatsapp_verified',object_type='DailyReport',object_id=str(day),changes={'as_of':cutoff.isoformat(),'reporting_cutoff':reporting_cutoff.isoformat(),'entries':entries,'devices':snapshots,'empty_confirmations':2})
        return entries
