"""Scoped IT inventory, approved procedures, recurrence management and automation UI."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError
from django.db.models import Count, Q, Sum, OuterRef, Subquery
from django.http import HttpResponse, FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from .models import (Company, Ticket, Asset, Maintenance, KnowledgeArticle, KnowledgeSuggestion, Problem,
    CompanySlaConfig, SlaAgentProfile, SlaAlert, AssetAttachment)
from .forms import AssetForm, MaintenanceForm, KnowledgeForm, ProblemForm, CompanySlaForm, SlaAgentForm
from .services import company_ids, audit
from .asset_uploads import prepare_attachment
from django.core.exceptions import ValidationError

staff = user_passes_test(lambda u:u.is_active and u.is_staff)
master = user_passes_test(lambda u:u.is_active and u.is_superuser)


def companies(user):
    return Company.objects.filter(pk__in=company_ids(user))


def assets_query(user):
    latest = Maintenance.objects.filter(asset_id=OuterRef('pk')).order_by('-date','-id')
    return Asset.objects.filter(company__in=companies(user)).select_related('company','contact').annotate(
        ticket_count=Count('tickets'),next_maintenance=Subquery(latest.values('next_date')[:1])).order_by('company__code','tag','pk')


@login_required
@staff
@never_cache
def assets(request):
    items = assets_query(request.user)
    if request.GET.get('company'):
        items = items.filter(company__code=request.GET['company'])
    if request.GET.get('q'):
        q = request.GET['q'][:150]
        items = items.filter(Q(tag__icontains=q)|Q(invoice_number__icontains=q)|Q(name__icontains=q)|Q(serial__icontains=q)|Q(location__icontains=q))
    return render(request,'helpdesk/assets.html',{'items':Paginator(items,30).get_page(request.GET.get('page')),
        'companies':companies(request.user),'asset_count':items.count(),
        'maintenance_due':items.filter(next_maintenance__lt=timezone.localdate()).exclude(state='retired').count()})


@login_required
@staff
@never_cache
def asset_detail(request,asset_id=None):
    item = get_object_or_404(assets_query(request.user),pk=asset_id) if asset_id else None
    maintenance = MaintenanceForm(request.POST if request.method=='POST' and request.POST.get('action')=='maintenance' else None)
    form = AssetForm(request.POST if request.method=='POST' and request.POST.get('action') not in ('maintenance', 'attachment') else None,
                     instance=item,companies=companies(request.user))
    if request.method=='POST' and request.POST.get('action')=='attachment':
        if not item:
            return HttpResponse(status=400)
        upload = request.FILES.get('file')
        kind = request.POST.get('kind')
        if not upload:
            messages.error(request, 'Selecione uma imagem ou PDF.')
        elif kind not in ('photo', 'signed_term', 'document'):
            messages.error(request, 'Tipo de anexo inválido.')
        else:
            try:
                content, filename = prepare_attachment(upload, kind)
                with transaction.atomic():
                    Asset.objects.select_for_update().get(pk=item.pk)
                    attachment = AssetAttachment(asset=item, kind=kind, original_name=filename,
                        size_bytes=content.size, uploaded_by=request.user.get_username())
                    attachment.file.save(content.name, content, save=False)
                    try:
                        attachment.save()
                    except Exception:
                        attachment.file.delete(save=False)
                        raise
                    audit(request.user.get_username(), 'support_asset_attachment', attachment,
                          {'asset':item.pk,'type':kind,'bytes':content.size})
                messages.success(request, 'Arquivo anexado e protegido por permissão.')
                return redirect(f'/support/assets/{item.pk}/')
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
    if request.method=='POST':
        if request.POST.get('action')=='maintenance' and item and maintenance.is_valid():
            with transaction.atomic():
                Asset.objects.select_for_update().get(pk=item.pk)
                record = maintenance.save(commit=False)
                record.asset,record.author = item,request.user.get_username()
                record.save()
                audit(request.user.get_username(),'support_maintenance',record,{'asset':item.pk,'date':str(record.date),'cost':str(record.cost)})
            messages.success(request,'Manutenção registrada no histórico do equipamento.')
            return redirect(f'/support/assets/{item.pk}/')
        elif request.POST.get('action') not in ('maintenance','attachment') and form.is_valid():
            try:
                with transaction.atomic():
                    Company.objects.select_for_update().get(pk=form.cleaned_data['company'].pk)
                    saved = form.save()
                    audit(request.user.get_username(),'support_asset',saved,{'fields':form.changed_data})
                messages.success(request,'Equipamento salvo.')
                return redirect(f'/support/assets/{saved.pk}/')
            except IntegrityError:
                form.add_error('tag','Essa identificação já existe nesta empresa.')
    return render(request,'helpdesk/asset_detail.html',{'item':item,'form':form,'maintenance_form':maintenance,
        'history':item.maintenance.all() if item else [], 'attachments':item.attachments.all() if item else [], 'tickets':item.tickets.order_by('-id')[:30] if item else []})


@login_required
@staff
@never_cache
def knowledge(request):
    items = KnowledgeArticle.objects.filter(company__in=companies(request.user)).select_related('company')
    if request.GET.get('q'):
        q=request.GET['q'][:150]
        items=items.filter(Q(title__icontains=q)|Q(keywords__icontains=q)|Q(procedure__icontains=q))
    if request.GET.get('company'):
        items=items.filter(company__code=request.GET['company'])
    return render(request,'helpdesk/knowledge.html',{'items':Paginator(items,24).get_page(request.GET.get('page')),
        'companies':companies(request.user),'suggestion_count':KnowledgeSuggestion.objects.filter(article__in=items).count(),
        'helpful_count':KnowledgeSuggestion.objects.filter(article__in=items,helpful=True).count()})


@login_required
@staff
@never_cache
def knowledge_detail(request,article_id=None):
    item = get_object_or_404(KnowledgeArticle,company__in=companies(request.user),pk=article_id) if article_id else None
    form = KnowledgeForm(request.POST or None,instance=item,companies=companies(request.user))
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            item=form.save(commit=False)
            item.author=request.user.get_username()
            item.save()
            audit(request.user.get_username(),'support_knowledge',item,{'published':item.published,'bot_enabled':item.bot_enabled,'fields':form.changed_data})
        messages.success(request,'Procedimento salvo. Somente artigos publicados e habilitados são sugeridos pelo bot.')
        return redirect(f'/support/knowledge/{item.pk}/')
    return render(request,'helpdesk/editor.html',{'title':'Procedimento da base de conhecimento','eyebrow':'Conhecimento aprovado',
        'form':form,'item':item,'back':'/support/knowledge/','preview':item.procedure if item else ''})


@login_required
@staff
@never_cache
def problems(request):
    scoped = Ticket.objects.filter(company__in=companies(request.user))
    if request.GET.get('company'):
        scoped=scoped.filter(company__code=request.GET['company'])
    records=Problem.objects.filter(company__in=companies(request.user)).select_related('company').annotate(ticket_count=Count('tickets'))
    if request.GET.get('company'):
        records=records.filter(company__code=request.GET['company'])
    repeated = scoped.values('company__code','target','category').annotate(total=Count('id')).filter(total__gte=2).order_by('-total','target')[:20]
    top_assets = Asset.objects.filter(company__in=companies(request.user),tickets__in=scoped).annotate(total=Count('tickets')).order_by('-total','tag')[:10]
    records=Paginator(records.order_by('-updated_at','-id'),20).get_page(request.GET.get('page'))
    return render(request,'helpdesk/problems.html',{'records':records,'items':records,'companies':companies(request.user),'repeated':repeated,
        'top_assets':top_assets,'reopened':scoped.filter(reopen_count__gt=0).order_by('-reopen_count','-id')[:20],
        'reopen_total':scoped.aggregate(n=Sum('reopen_count'))['n'] or 0})


@login_required
@staff
@never_cache
def problem_detail(request,problem_id=None):
    item=get_object_or_404(Problem,company__in=companies(request.user),pk=problem_id) if problem_id else None
    form=ProblemForm(request.POST or None,instance=item,companies=companies(request.user))
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            item=form.save(commit=False)
            item.author=request.user.get_username()
            item.save()
            audit(request.user.get_username(),'support_problem',item,{'fields':form.changed_data})
        messages.success(request,'Problema recorrente salvo. Vincule os chamados relacionados no formulário de atendimento.')
        return redirect(f'/support/problems/{item.pk}/')
    return render(request,'helpdesk/editor.html',{'title':'Gestão do problema recorrente','eyebrow':'Recorrências e causa raiz',
        'form':form,'item':item,'back':'/support/problems/','linked_tickets':item.tickets.order_by('-id')[:30] if item else []})


@login_required
@master
@never_cache
def company_sla(request,code):
    company=get_object_or_404(Company,code=code)
    config,_=CompanySlaConfig.objects.get_or_create(company=company)
    form=CompanySlaForm(request.POST or None,instance=config)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            CompanySlaConfig.objects.select_for_update().get(pk=config.pk)
            saved=form.save()
            audit(request.user.get_username(),'support_company_sla',saved,{'company':code,'fields':form.changed_data,
                'mode':saved.mode,'schedule':saved.schedule,'holidays':saved.holidays,'manager_id':saved.manager_id})
        messages.success(request,'Expediente e automações salvos. Calendários dos chamados já iniciados são preservados.')
        return redirect(f'/support/sla/company/{code}/')
    profiles=SlaAgentProfile.objects.select_related('user','contact')
    return render(request,'helpdesk/company_sla.html',{'company':company,'form':form,'profiles':profiles})


@login_required
@master
@never_cache
def agents(request,profile_id=None):
    profile=get_object_or_404(SlaAgentProfile,pk=profile_id) if profile_id else None
    form=SlaAgentForm(request.POST or None,instance=profile)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            saved=form.save()
            audit(request.user.get_username(),'support_sla_person',saved,{'user_id':saved.user_id,'contact_id':saved.contact_id})
        messages.success(request,'WhatsApp do técnico/gestor vinculado para os avisos de TI.')
        return redirect('/support/sla/agents/')
    return render(request,'helpdesk/agents.html',{'form':form,'profiles':SlaAgentProfile.objects.select_related('user','contact').order_by('user__username')})


@login_required
@staff
@never_cache
def alerts(request):
    items=SlaAlert.objects.filter(company__in=companies(request.user)).select_related('company','ticket','recipient')
    if request.method=='POST':
        if not request.user.is_superuser:
            return HttpResponse(status=403)
        value=request.POST.get('alert','')
        if not value.isascii() or not value.isdigit() or len(value)>18:
            return HttpResponse(status=400)
        item=get_object_or_404(items,pk=value)
        with transaction.atomic():
            item=SlaAlert.objects.select_for_update().get(pk=item.pk)
            if item.status in ('failed','blocked'):
                item.status='blocked'
                item.attempts=0
                item.detail='Revalidação solicitada pelo administrador.'
                item.save(update_fields=['status','attempts','detail'])
                audit(request.user.get_username(),'support_sla_retry',item,{'key':item.key})
                messages.success(request,'Aviso será revalidado no próximo ciclo. Resultados incertos não são repetidos automaticamente.')
        return redirect('/support/sla/alerts/')
    if request.GET.get('company'):
        items=items.filter(company__code=request.GET['company'])
    if request.GET.get('kind')=='weekly':
        items=items.filter(kind='weekly')
    return render(request,'helpdesk/sla_alerts.html',{'items':Paginator(items.order_by('-id'),30).get_page(request.GET.get('page')),
        'companies':companies(request.user),'pending_count':items.filter(status__in=('pending','blocked')).count()})


@login_required
@staff
@never_cache
def asset_attachment(request, attachment_id):
    attachment = get_object_or_404(
        AssetAttachment.objects.select_related('asset'),
        pk=attachment_id, asset__company__in=companies(request.user))
    try:
        stream = attachment.file.open('rb')
    except (OSError, ValueError):
        raise Http404('Arquivo indisponível')
    response = FileResponse(stream, as_attachment=True, filename=attachment.original_name)
    response['Content-Type'] = 'image/webp' if attachment.kind == 'photo' else 'application/pdf'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response
