import io,json,math
from django import forms
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse,JsonResponse
from django.shortcuts import render,redirect
from django.contrib import messages
from PIL import Image,UnidentifiedImageError
from .models import AccessMap,Camera,AuditLog

class PlanForm(forms.Form):
    plan=forms.FileField(label='Planta do local (PNG, JPEG ou WebP; até 2 MB)')
    def clean_plan(self):
        value=self.cleaned_data['plan']
        if value.size>2*1024*1024:raise forms.ValidationError('A imagem deve ter até 2 MB.')
        try:
            with Image.open(value) as image:
                if image.format not in ('PNG','JPEG','WEBP') or image.width*image.height>24000000:raise ValueError()
                image.verify()
            value.seek(0)
            # Re-encode to remove metadata, embedded content and animations.
            with Image.open(value) as image:
                image=image.convert('RGB');image.thumbnail((2400,1600))
                out=io.BytesIO();image.save(out,format='JPEG',quality=88)
            return out.getvalue()
        except (OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):raise forms.ValidationError('Use uma imagem PNG, JPEG ou WebP válida, até 24 megapixels.')

@login_required
def map_page(request):
    layout=AccessMap.objects.filter(pk=1).first();form=PlanForm()
    if request.method=='POST':
        if not request.user.is_staff:return HttpResponse(status=403)
        form=PlanForm(request.POST,request.FILES)
        if form.is_valid():
            with transaction.atomic():
                layout,_=AccessMap.objects.select_for_update().get_or_create(pk=1)
                layout.image=form.cleaned_data['plan'];layout.image_type='image/jpeg';layout.save()
                AuditLog.objects.create(actor=request.user.get_username(),action='map_image',object_type='AccessMap',object_id='1',changes={'image':'updated'})
            messages.success(request,'Planta atualizada. Posicione os acessos e salve o mapa.');return redirect('/map/')
    ratio=1.6
    if layout and layout.image:
        with Image.open(io.BytesIO(bytes(layout.image))) as picture:ratio=picture.width/picture.height
    response=render(request,'access_map.html',{'plan_ratio':ratio,'plan_form':form,'positions':layout.positions if layout else {},'has_plan':bool(layout and layout.image),'cameras':Camera.objects.filter(enabled=True)})
    response['Cache-Control']='private, no-store';return response

@login_required
def map_positions(request):
    if request.method!='POST':return HttpResponse(status=405)
    if not request.user.is_staff:return HttpResponse(status=403)
    try:
        body=json.loads(request.body);positions=body['positions']
        if not isinstance(positions,dict) or len(positions)>500:raise ValueError()
        ids={str(pk) for pk in Camera.objects.values_list('pk',flat=True)}
        for key,pos in positions.items():
            if key not in ids or not isinstance(pos,dict) or set(pos)!= {'x','y'}:raise ValueError()
            if any(type(pos[k]) not in (int,float) or not math.isfinite(pos[k]) or not 0<=pos[k]<=100 for k in ('x','y')):raise ValueError()
        with transaction.atomic():
            layout,_=AccessMap.objects.select_for_update().get_or_create(pk=1)
            layout.positions={k:{axis:round(v[axis],2) for axis in ('x','y')} for k,v in positions.items()};layout.save()
            AuditLog.objects.create(actor=request.user.get_username(),action='map_positions',object_type='AccessMap',object_id='1',changes={'devices':sorted(positions)})
        return JsonResponse({'saved':True})
    except (ValueError,TypeError,KeyError):return JsonResponse({'error':'Posições inválidas.'},status=400)

@login_required
def map_image(request):
    layout=AccessMap.objects.filter(pk=1).only('image','image_type').first()
    if not layout or not layout.image:return HttpResponse(status=404)
    response=HttpResponse(bytes(layout.image),content_type=layout.image_type)
    response['Cache-Control']='private, no-store';response['X-Content-Type-Options']='nosniff';return response
