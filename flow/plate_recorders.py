"""NVD 3116 channel profiles. RTSP credentials stay on the local collector."""
import hashlib
import secrets
from django import forms
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods
from .plate_status import recorder_status, camera_status
from .models import PlateRecorder, PlateCamera, AuditLog

class RecorderForm(forms.ModelForm):
    class Meta:
        model = PlateRecorder
        fields = ['name','host','port','enabled']
        labels = {'name':'Nome do NVR','host':'IP do NVR','port':'Porta RTSP','enabled':'NVR ativo'}
    def clean_port(self):
        port=self.cleaned_data['port']
        if not 1<=port<=65535: raise forms.ValidationError('Porta entre 1 e 65535.')
        return port

class ChannelForm(forms.Form):
    enabled=forms.BooleanField(required=False,label='Ler placas neste canal')
    name=forms.CharField(max_length=70,label='Identificação',required=False)
    stream=forms.ChoiceField(choices=[('0','Principal (recomendado)'),('1','Extra')],label='Vídeo')
    x=forms.FloatField(min_value=0,max_value=1,label='X')
    y=forms.FloatField(min_value=0,max_value=1,label='Y')
    width=forms.FloatField(min_value=0.001,max_value=1,label='Largura')
    height=forms.FloatField(min_value=0.001,max_value=1,label='Altura')
    def clean(self):
        d=super().clean()
        if all(k in d for k in ('x','y','width','height')):
            if d['x']+d['width']>1 or d['y']+d['height']>1: raise forms.ValidationError('A área de leitura deve ficar dentro da imagem.')
        return d

@login_required
@user_passes_test(lambda u:u.is_superuser)
@require_http_methods(['GET','POST'])
def recorders(request,pk=None):
    obj=get_object_or_404(PlateRecorder,pk=pk) if pk else None
    token=None
    if request.method=='POST' and request.POST.get('action')=='rotate' and obj:
        token=secrets.token_urlsafe(32)
        obj.token_digest=hashlib.sha256(token.encode()).hexdigest()
        obj.save(update_fields=['token_digest'])
        AuditLog.objects.create(actor=request.user.username,action='update',object_type='PlateRecorder',object_id=str(obj.pk),changes={'token_rotated':True})
        response=render(request,'plate_recorders.html',{'form':RecorderForm(instance=obj),'editing':obj,'recorders':with_status(),'token':token})
        response['Cache-Control']='private, no-store'
        return response
    form=RecorderForm(request.POST or None,instance=obj)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            recorder=form.save(commit=False)
            if obj is None:
                token=secrets.token_urlsafe(32)
                recorder.token_digest=hashlib.sha256(token.encode()).hexdigest()
            recorder.save()
            AuditLog.objects.create(actor=request.user.username,action='update' if obj else 'create',object_type='PlateRecorder',object_id=str(recorder.pk),changes={'enabled':recorder.enabled,'host':recorder.host,'port':recorder.port})
        if obj: return redirect('/plates/nvrs/')
        form=RecorderForm()
    response=render(request,'plate_recorders.html',{'form':form,'editing':obj,'recorders':with_status(),'token':token})
    response['Cache-Control']='private, no-store'
    return response

@login_required
@user_passes_test(lambda u:u.is_superuser)
@require_http_methods(['GET','POST'])
def channels(request,pk):
    recorder=get_object_or_404(PlateRecorder,pk=pk)
    existing={c.channel:c for c in PlateCamera.objects.filter(recorder=recorder)}
    rows=[]
    for number in range(1,17):
        camera=existing.get(number)
        roi=camera.roi if camera and len(camera.roi)==4 else [0,0,1,1]
        initial={'enabled':camera.enabled if camera else False,'name':camera.name.split(' · ',1)[-1] if camera else f'Canal {number}', 'stream':str(camera.stream) if camera else '0','x':roi[0],'y':roi[1],'width':roi[2],'height':roi[3]}
        rows.append({'number':number,'status':camera_status(camera) if camera else None,'camera':camera,'form':ChannelForm(request.POST if request.method=='POST' else None,prefix=f'ch{number}',initial=initial)})
    if request.method=='POST' and all([row['form'].is_valid() for row in rows]):
        with transaction.atomic():
            for row in rows:
                d=row['form'].cleaned_data; number=row['number']; camera=row['camera']
                # Unselected channels do not create cameras or consume video sessions.
                if not camera and not d['enabled']: continue
                if camera is None:
                    camera=PlateCamera(recorder=recorder,channel=number,token_digest=hashlib.sha256(secrets.token_bytes(32)).hexdigest())
                camera.name=(f'NVR {recorder.pk}/CH {number} · '+(d['name'] or f'Canal {number}'))[:100]
                camera.enabled=d['enabled']; camera.stream=int(d['stream'])
                camera.roi=[d[k] for k in ('x','y','width','height')]
                camera.save()
            AuditLog.objects.create(actor=request.user.username,action='update',object_type='PlateRecorder',object_id=str(recorder.pk),changes={'enabled_channels':[r['number'] for r in rows if r['form'].cleaned_data['enabled']]})
        return redirect(f'/plates/nvrs/{pk}/channels/')
    response=render(request,'plate_channels.html',{'recorder':recorder,'rows':rows,'connection':recorder_status(recorder)})
    response['Cache-Control']='private, no-store'
    return response

@require_GET
def collector_config(request):
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip()
    recorder=PlateRecorder.objects.filter(token_digest=hashlib.sha256(token.encode()).hexdigest()).first() if token else None
    if not recorder: return JsonResponse({'error':'Token inválido'},status=401)
    PlateRecorder.objects.filter(pk=recorder.pk).update(last_seen=timezone.now())
    cameras=PlateCamera.objects.filter(recorder=recorder,enabled=True) if recorder.enabled else PlateCamera.objects.none()
    response=JsonResponse({'recorder':{'id':recorder.pk,'name':recorder.name,'host':recorder.host,'port':recorder.port,'model':'Intelbras NVD 3116','enabled':recorder.enabled},'channels':[{'channel':c.channel,'name':c.name,'stream':c.stream,'roi':c.roi or [0,0,1,1]} for c in cameras.order_by('channel')]})
    response['Cache-Control']='private, no-store'
    return response


def with_status():
    recorders=list(PlateRecorder.objects.prefetch_related('platecamera_set').all())
    for nvr in recorders: nvr.connection=recorder_status(nvr)
    return recorders

@login_required
@user_passes_test(lambda u:u.is_superuser)
@require_GET
def status(request):
    response=JsonResponse({'recorders':[n.connection for n in with_status()]})
    response['Cache-Control']='private, no-store'
    return response
