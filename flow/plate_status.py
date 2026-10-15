from datetime import timedelta
from django.utils import timezone

WINDOW=timedelta(seconds=45)
def stamp(value):
    return timezone.localtime(value).strftime('%d/%m/%Y %H:%M:%S') if value else 'Nenhum'

def camera_status(camera,now=None):
    now=now or timezone.now()
    if not camera.enabled or (camera.recorder_id and not camera.recorder.enabled):
        state='disabled';label='Desativado'
    elif not camera.video_checked_at:
        state='pending';label='Aguardando teste de vídeo pelo coletor'
    elif now-camera.video_checked_at>WINDOW:
        state='stale';label='Sem atualização do coletor'
    elif camera.video_status=='streaming' and camera.last_frame_at and now-camera.last_frame_at<=timedelta(seconds=15):
        state='streaming';label='Recebendo vídeo do NVR' if camera.recorder_id else 'Recebendo vídeo da câmera'
    else:
        state='unavailable';label='Sem vídeo — confira IP, porta, credenciais e canal no coletor'
    return {'id':camera.pk,'channel':camera.channel,'state':state,'label':label,'checked_at':stamp(camera.video_checked_at),'last_frame_at':stamp(camera.last_frame_at)}

def recorder_status(recorder,cameras=None):
    now=timezone.now()
    cameras=list(cameras if cameras is not None else recorder.platecamera_set.all())
    statuses=[camera_status(c,now) for c in cameras]
    active=[s for s,c in zip(statuses,cameras) if c.enabled]
    online=sum(s['state']=='streaming' for s in active)
    collector='Conectado ao painel' if recorder.last_seen and now-recorder.last_seen<=WINDOW else ('Sem contato recente com o painel' if recorder.last_seen else 'Aguardando coletor local')
    if not recorder.enabled:state='disabled';label='NVR desativado'
    elif not active:state='pending';label='Nenhum canal selecionado'
    elif online==len(active):state='streaming';label=f'Vídeo conectado · {online}/{len(active)} canais'
    elif online:state='partial';label=f'Conexão parcial · {online}/{len(active)} canais com vídeo'
    elif any(s['state']=='unavailable' for s in active):state='unavailable';label='Sem vídeo nos canais selecionados'
    elif any(s['state']=='stale' for s in active):state='stale';label='Sem atualização de vídeo do coletor'
    else:state='pending';label='Aguardando teste de vídeo pelo coletor'
    return {'id':recorder.pk,'state':state,'label':label,'collector':collector,'last_seen':stamp(recorder.last_seen),'channels':statuses}
