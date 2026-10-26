"""Asset life-cycle operations, authenticated documents and audit dashboard."""
from io import BytesIO
from datetime import timedelta
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Count, Sum, Q
from django.http import HttpResponse, FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen.canvas import Canvas
import qrcode
from .models import Asset, AssetMovement, InventoryCheck, AssetDisposal, Maintenance
from .forms import AssetMovementForm, InventoryCheckForm, AssetDisposalForm
from .services import company_ids, audit

staff = user_passes_test(lambda u: u.is_active and u.is_staff)
master = user_passes_test(lambda u: u.is_active and u.is_superuser)


def scoped(request, pk):
    return get_object_or_404(Asset.objects.select_related('company','contact').filter(
        company_id__in=company_ids(request.user)), pk=pk)


@login_required
@staff
@never_cache
def overview(request):
    assets = Asset.objects.filter(company_id__in=company_ids(request.user))
    if request.GET.get('company'):
        assets = assets.filter(company__code=request.GET['company'])
    totals = {k: assets.filter(state=k).count() for k in ('active','stock','maintenance','retired')}
    total = assets.count()
    unassigned = assets.filter(contact__isnull=True).exclude(state='retired').count()
    warranty_soon = assets.filter(warranty_expires_at__range=(
        timezone.localdate(),timezone.localdate()+timedelta(days=60))).count()
    missing_checks = assets.exclude(state='retired').exclude(
        checks__checked_at__date__gte=timezone.localdate()-timedelta(days=365)).distinct().count()
    cost = Maintenance.objects.filter(asset__in=assets).aggregate(total=Sum('cost'))['total'] or Decimal('0')
    return render(request,'helpdesk/asset_dashboard.html',{
        'totals':totals,'total':total,'unassigned':unassigned,'warranty_soon':warranty_soon,
        'missing_checks':missing_checks,'cost':cost,
        'companies':assets.values('company__code').annotate(total=Count('id')).order_by('company__code'),
        'recent':assets.order_by('-created_at')[:15]})


@login_required
@staff
@never_cache
def movement(request, pk):
    asset=scoped(request,pk)
    allowed=Asset.objects.filter(company_id__in=company_ids(request.user))
    from .models import Company
    destinations=Company.objects.filter(pk__in=company_ids(request.user))
    form=AssetMovementForm(request.POST or None, companies=destinations,
        initial={'company':asset.company_id,'contact':asset.contact_id,'location':asset.location})
    if request.method=='POST' and form.is_valid():
        data=form.cleaned_data
        if asset.state=='retired':
            form.add_error(None,'Patrimônio baixado não pode ser transferido.')
        elif data['company'].pk!=asset.company_id and not request.user.is_superuser:
            form.add_error('company','Transferência entre empresas exige aprovação do master.')
        else:
            with transaction.atomic():
                current=Asset.objects.select_for_update().get(pk=asset.pk)
                if current.state=='retired':
                    return HttpResponse('Equipamento baixado.',status=409)
                if current.company_id!=asset.company_id:
                    return HttpResponse('Cadastro alterado; recarregue.',status=409)
                if current.company_id!=data['company'].pk and Asset.objects.filter(company=data['company'],tag=current.tag).exists():
                    form.add_error('company','Patrimônio já existe na empresa de destino.')
                else:
                    event=AssetMovement.objects.create(asset=current,action=data['action'],
                        old_company=current.company,new_company=data['company'],
                        old_contact=current.contact,new_contact=data['contact'],
                        old_location=current.location,new_location=data['location'],
                        reason=data['reason'],author=request.user.get_username())
                    current.company=data['company']
                    current.contact=data['contact']
                    current.location=data['location']
                    current.save(update_fields=['company','contact','location'])
                    audit(request.user.get_username(),'support_asset_movement',event,{
                        'asset':current.pk,'old_company':event.old_company.code,'new_company':event.new_company.code})
                    messages.success(request,'Movimentação registrada com histórico.')
                    return redirect(f'/support/assets/{asset.pk}/')
    return render(request,'helpdesk/asset_action.html',{'asset':asset,'form':form,'title':'Transferência / entrega / devolução'})


@login_required
@staff
@never_cache
def check(request, pk):
    asset=scoped(request,pk)
    form=InventoryCheckForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        data=form.cleaned_data
        with transaction.atomic():
            current=Asset.objects.select_for_update().get(pk=asset.pk)
            event=InventoryCheck.objects.create(asset=current,found=data['found'],
                location=data['location'],notes=data['notes'],checked_by=request.user.get_username())
            audit(request.user.get_username(),'support_inventory_check',event,{'asset':current.pk,'found':event.found})
        messages.success(request,'Conferência física registrada.')
        return redirect(f'/support/assets/{asset.pk}/')
    return render(request,'helpdesk/asset_action.html',{'asset':asset,'form':form,'title':'Conferência física'})


@login_required
@master
@never_cache
def disposal(request, pk):
    asset=scoped(request,pk)
    form=AssetDisposalForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            current=Asset.objects.select_for_update().get(pk=asset.pk)
            if current.state=='retired' or AssetDisposal.objects.filter(asset=current).exists():
                return HttpResponse('Patrimônio já baixado.',status=409)
            data=form.cleaned_data
            if current.kind in ('computer','server') and not data['data_sanitized']:
                form.add_error('data_sanitized','Exigida confirmação de sanitização para equipamentos com armazenamento.')
            else:
                event=AssetDisposal.objects.create(asset=current,reason=data['reason'],
                    data_sanitized=data['data_sanitized'],approved_by=request.user.get_username(),
                    disposed_on=data['disposed_on'])
                current.state='retired'
                current.disposed_at=data['disposed_on']
                current.disposal_reason=data['reason']
                current.save(update_fields=['state','disposed_at','disposal_reason'])
                audit(request.user.get_username(),'support_asset_disposal',event,{'asset':current.pk})
                messages.success(request,'Baixa patrimonial registrada e auditada.')
                return redirect(f'/support/assets/{asset.pk}/')
    return render(request,'helpdesk/asset_action.html',{'asset':asset,'form':form,'title':'Baixa patrimonial'})


@login_required
@staff
@never_cache
def qr_code(request, pk):
    asset=scoped(request,pk)
    url=request.build_absolute_uri(f'/support/assets/{asset.pk}/')
    img=qrcode.make(url)
    buf=BytesIO()
    img.save(buf,format='PNG')
    response=HttpResponse(buf.getvalue(),content_type='image/png')
    response['Cache-Control']='private, no-store'
    return response


def _line(canvas, label, value, y):
    text=f'{label}: {value or "Não informado"}'
    for fragment in simpleSplit(text,'Helvetica',10,500):
        canvas.drawString(52,y,fragment)
        y-=17
    return y-5


@login_required
@staff
@never_cache
def responsibility_term(request, pk):
    asset=scoped(request,pk)
    buffer=BytesIO()
    pdf=Canvas(buffer,pagesize=A4)
    pdf.setTitle('GesTi - Termo de responsabilidade patrimonial')
    pdf.setFont('Helvetica-Bold',16)
    pdf.drawString(52,790,'TERMO DE RESPONSABILIDADE PATRIMONIAL')
    pdf.setFont('Helvetica',10)
    y=751
    for label,value in [
        ('Empresa',asset.company.name),('Código do ativo / Patrimônio',asset.tag),
        ('NF',asset.invoice_number),('Produto',asset.name),('Data de aquisição',str(asset.acquisition_date or '')),
        ('CÓD PRPOD',asset.product_code),('Modelo / Série',asset.serial),
        ('Localização',asset.location),('Colaborador responsável',asset.contact.name if asset.contact else ''),
        ('Data do termo',timezone.localdate().strftime('%d/%m/%Y'))]:
        y=_line(pdf,label,value,y)
    y-=25
    pdf.setFont('Helvetica',10)
    for line in [
        'Declaro ter recebido o equipamento descrito, assumindo o dever de',
        'conservação, uso profissional e devolução conforme as políticas internas.',
        'Ocorrências, danos ou extravios deverão ser comunicados à empresa.']:
        pdf.drawString(52,y,line);y-=18
    y-=65
    pdf.line(52,y,270,y)
    pdf.line(325,y,540,y)
    pdf.drawString(52,y-17,'Responsável pelo equipamento')
    pdf.drawString(325,y-17,'Entrega / TI')
    pdf.save()
    response=HttpResponse(buffer.getvalue(),content_type='application/pdf')
    response['Content-Disposition']=f'attachment; filename="termo-patrimonio-{asset.pk}.pdf"'
    response['Cache-Control']='private, no-store'
    return response
