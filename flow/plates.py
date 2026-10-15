import hashlib
import io
import math
import re
import secrets
import uuid
from datetime import date, timedelta
from PIL import Image, UnidentifiedImageError
from django import forms
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from .models import PlateCamera, PlateCapture, AuditLog, PlateRecorder

PLATE = re.compile(r'(?:[A-Z]{3}[0-9]{4}|[A-Z]{3}[0-9][A-Z][0-9]{2})')
def normalize(value):
    value = str(value).upper().replace('-', '').replace(' ', '')
    if not PLATE.fullmatch(value): raise ValueError('Placa inválida')
    return value

class Filters(forms.Form):
    plate = forms.CharField(required=False, max_length=7, label='Placa ou parte')
    camera = forms.ModelChoiceField(required=False, queryset=PlateCamera.objects.all(), label='Cancela')
    start = forms.DateField(required=False, label='De', widget=forms.DateInput(attrs={'type':'date'}))
    end = forms.DateField(required=False, label='Até', widget=forms.DateInput(attrs={'type':'date'}))

@login_required
@require_GET
def index(request):
    form = Filters(request.GET)
    rows = PlateCapture.objects.select_related('camera').all()
    if form.is_valid():
        d = form.cleaned_data
        if d['plate']: rows = rows.filter(plate__contains=d['plate'].upper())
        if d['camera']: rows = rows.filter(camera=d['camera'])
        if d['start']: rows = rows.filter(captured_at__date__gte=d['start'])
        if d['end']: rows = rows.filter(captured_at__date__lte=d['end'])
    else: rows = rows.none()
    page = Paginator(rows, 30).get_page(request.GET.get('page'))
    query = request.GET.copy(); query.pop('page', None)
    response = render(request, 'plates.html', {'form':form, 'page':page, 'query':query.urlencode(), 'cameras':PlateCamera.objects.all()})
    response['Cache-Control'] = 'no-store'
    return response

@login_required
@require_GET
def photo(request, pk):
    obj = get_object_or_404(PlateCapture, pk=pk)
    response = HttpResponse(bytes(obj.image), content_type='image/jpeg')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response

@login_required
@user_passes_test(lambda u:u.is_staff)
@require_POST
def review(request, pk):
    obj = get_object_or_404(PlateCapture, pk=pk)
    try: plate = normalize(request.POST.get('plate', ''))
    except ValueError: return HttpResponse('Placa inválida', status=400)
    old = obj.plate
    obj.plate = plate; obj.reviewed = True; obj.save(update_fields=['plate','reviewed'])
    AuditLog.objects.create(actor=request.user.username, action='update', object_type='PlateCapture', object_id=str(obj.pk), changes={'plate':{'before':old,'after':plate}})
    return redirect('/plates/')

@login_required
@user_passes_test(lambda u:u.is_superuser)
def cameras(request):
    token = None; error = None
    if request.method == 'POST':
        if request.POST.get('camera'):
            camera = get_object_or_404(PlateCamera, pk=request.POST['camera'])
            if request.POST.get('action') == 'toggle':
                camera.enabled = not camera.enabled
            else:
                token = secrets.token_urlsafe(32)
                camera.token_digest = hashlib.sha256(token.encode()).hexdigest()
            camera.save()
        else:
            name = request.POST.get('name', '').strip()
            if not name or len(name)>100 or PlateCamera.objects.filter(name=name).exists(): error = 'Informe um nome único com até 100 caracteres.'
            else:
                token = secrets.token_urlsafe(32)
                camera = PlateCamera.objects.create(name=name, token_digest=hashlib.sha256(token.encode()).hexdigest())
        if not error:
            AuditLog.objects.create(actor=request.user.username, action='update', object_type='PlateCamera', object_id=str(camera.pk), changes={'enabled':camera.enabled, 'token_rotated':bool(token)})
    response = render(request, 'plate_cameras.html', {'cameras':PlateCamera.objects.all(), 'token':token, 'error':error})
    response['Cache-Control'] = 'no-store'
    return response

@csrf_exempt
@require_POST
def ingest(request):
    camera = camera_access(request)
    if isinstance(camera, JsonResponse): return camera
    try:
        plate = normalize(request.POST.get('plate',''))
        event = uuid.UUID(request.POST.get('event_id',''))
        captured = parse_datetime(request.POST.get('captured_at',''))
        confidence = float(request.POST.get('confidence',''))
        if not captured or timezone.is_naive(captured) or captured>timezone.now()+timedelta(minutes=5): raise ValueError()
        if not math.isfinite(confidence) or not 0<=confidence<=1: raise ValueError()
        upload = request.FILES.get('image')
        if not upload or upload.size>500_000: raise ValueError()
        raw = upload.read()
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>8_000_000: raise ValueError()
            image.load(); image.thumbnail((1280,960))
            output = io.BytesIO(); image.convert('RGB').save(output, format='JPEG', quality=85)
        obj, created = PlateCapture.objects.get_or_create(camera=camera, event_id=event, defaults={'plate':plate,'captured_at':captured,'confidence':confidence,'image':output.getvalue()})
        PlateCamera.objects.filter(pk=camera.pk).update(last_seen=timezone.now())
        return JsonResponse({'id':obj.pk, 'created':created}, status=201 if created else 200)
    except (ValueError, TypeError, UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return JsonResponse({'error':'Envie placa brasileira válida, UUID, data ISO com fuso, confiança 0–1 e imagem até 500 KB.'}, status=400)


def camera_access(request):
    token = request.headers.get('Authorization','').removeprefix('Bearer ').strip()
    camera = PlateCamera.objects.filter(token_digest=hashlib.sha256(token.encode()).hexdigest(), enabled=True).first() if token else None
    if not camera:
        recorder = PlateRecorder.objects.filter(token_digest=hashlib.sha256(token.encode()).hexdigest(), enabled=True).first() if token else None
        if recorder:
            try: channel = int(request.POST.get('channel',''))
            except (ValueError,TypeError): return JsonResponse({'error':'Canal inválido'},status=400)
            camera = PlateCamera.objects.filter(recorder=recorder,channel=channel,enabled=True).first()
    if not camera: return JsonResponse({'error':'Token inválido ou canal desativado'}, status=401)
    if camera.recorder_id and not camera.recorder.enabled:
        return JsonResponse({'error':'NVR desativado'},status=401)
    return camera

@csrf_exempt
@require_POST
def heartbeat(request):
    camera = camera_access(request)
    if isinstance(camera, JsonResponse): return camera
    try:
        age_text=request.POST.get('frame_age','')
        age=float(age_text) if age_text else None
        if age is not None and (not math.isfinite(age) or not 0<=age<=86400): raise ValueError()
    except (ValueError,TypeError): return JsonResponse({'error':'Idade do quadro inválida'},status=400)
    now=timezone.now()
    values={'video_checked_at':now,'video_status':'streaming' if age is not None and age<=3 else 'unavailable'}
    if age is not None: values['last_frame_at']=now-timedelta(seconds=age)
    PlateCamera.objects.filter(pk=camera.pk).update(**values)
    return JsonResponse({'status':values['video_status']})
