from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from .models import NetworkAsset, NetworkLink, Site
from .forms import NetworkAssetForm

@staff_member_required
def asset_create(request):
    form = NetworkAssetForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        asset = form.save()
        messages.success(request, f'Equipamento “{asset.name}” cadastrado. Configure o segredo no ambiente do servidor antes de habilitar a coleta.')
        return redirect('netcontrol-dashboard')
    return render(request, 'netcontrol/asset_form.html', {'form': form})

@staff_member_required
def dashboard(request):
    sites = Site.objects.all()
    assets = NetworkAsset.objects.select_related('site').all()
    selected = request.GET.get('site','')
    if selected.isdecimal():
        assets = assets.filter(site_id=int(selected))
    ids = list(assets.values_list('pk',flat=True))
    links = NetworkLink.objects.filter(source_id__in=ids,target_id__in=ids).select_related('source','target','source_interface')
    return render(request,'netcontrol/dashboard.html',{
        'sites':sites,'assets':assets,'links':links,'selected':selected,
        'total':len(ids),
        'online':assets.filter(last_status='online').count(),
        'unknown':assets.filter(last_status='unknown').count(),
    })

@staff_member_required
def topology_json(request):
    assets = NetworkAsset.objects.all()
    selected = request.GET.get('site','')
    if selected.isdecimal(): assets=assets.filter(site_id=int(selected))
    nodes=list(assets.values('id','name','kind','ip','last_status','location','site_id'))
    ids=[n['id'] for n in nodes]
    links=list(NetworkLink.objects.filter(source_id__in=ids,target_id__in=ids).values('source_id','target_id','evidence','source_interface__name'))
    return JsonResponse({'nodes':nodes,'links':links})
