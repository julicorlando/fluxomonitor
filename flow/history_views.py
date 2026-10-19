import os
from datetime import timedelta
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required,user_passes_test
from django.shortcuts import render,redirect
from django.http import JsonResponse
from django.db import transaction
from django.utils import timezone
from .models import Camera,HistoryImport,AuditLog,ImportCleanup
from .services import ZONE

class ImportForm(forms.ModelForm):
    class Meta:
        model=HistoryImport
        fields=['camera','start','end']
        labels={'camera':'Dispositivo','start':'Data inicial','end':'Data final'}
        widgets={'start':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}),'end':forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'})}
    def __init__(self,*a,**kwargs):
        super().__init__(*a,**kwargs)
        ips=[v.strip() for v in os.environ.get('PFC_PULL_IPS','').split(',') if v.strip()]
        self.fields['camera'].queryset=Camera.objects.filter(enabled=True,ip__in=ips)
    def clean(self):
        data=super().clean();start=data.get('start');end=data.get('end')
        today=timezone.now().astimezone(ZONE).date()
        if start and end and (start>end or end>today or start<today-timedelta(days=89)):
            raise forms.ValidationError('Escolha um período dentro dos últimos 90 dias, até hoje.')
        camera=data.get('camera')
        if camera and camera.ip=='192.168.1.237':
            data['camera']=Camera.objects.filter(ip=camera.ip,enabled=True).order_by('replica_slot','id').first()
        return data

@login_required
@user_passes_test(lambda u:u.is_staff)
def imports(request):
    today=timezone.now().astimezone(ZONE).date()
    form=ImportForm(initial={'start':today-timedelta(days=89),'end':today})
    if request.method=='POST':
        if request.POST.get('action') in ('cleanup','restore','reimport_today'):
            if not request.user.is_superuser:
                from django.http import HttpResponseForbidden
                return HttpResponseForbidden()
            from .import_cleanup import cleanup,restore,reimport_today
            try:
                if request.POST['action']=='reimport_today':reimport_today(request.user.get_username())
                elif request.POST['action']=='cleanup':cleanup(request.user.get_username())
                else:restore(int(request.POST.get('archive_id','0')),request.user.get_username())
                messages.success(request,'Operação concluída com cópia recuperável.')
            except ValueError as exc:messages.error(request,str(exc))
            except Exception as exc:
                import logging
                logging.error('Ação de importação bloqueada: %s',type(exc).__name__)
                messages.error(request,'Operação não concluída: '+type(exc).__name__+'. A transação não foi aplicada.')
            return redirect('/imports/')
        if request.POST.get('action') in ('pause','resume'):
            with transaction.atomic():
                candidate=HistoryImport.objects.filter(pk=request.POST.get('job_id')).first()
                if candidate:
                    Camera.objects.select_for_update().get(pk=candidate.camera_id)
                    job=HistoryImport.objects.select_for_update().get(pk=candidate.pk)
                    action=request.POST['action']
                    if action=='pause' and job.status in ('pending','running'):
                        job.status='paused';job.save(update_fields=['status','updated_at'])
                    elif action=='resume' and job.status=='paused':
                        if HistoryImport.objects.filter(camera_id=job.camera_id,status__in=['pending','running']).exclude(pk=job.pk).exists():
                            messages.error(request,'Há outra tarefa ativa neste equipamento. Aguarde ou pause essa tarefa antes de retomar.')
                            return redirect('/imports/')
                        job.status='pending';job.save(update_fields=['status','updated_at'])
                    else:return redirect('/imports/')
                    AuditLog.objects.create(actor=request.user.get_username(),action='history_'+action,object_type='HistoryImport',object_id=str(job.pk),changes={'offset':job.offset})
            return redirect('/imports/')
        if request.POST.get('action')=='retry':
            with transaction.atomic():
                candidate=HistoryImport.objects.filter(pk=request.POST.get('job_id')).first()
                if candidate:
                    camera=Camera.objects.select_for_update().get(pk=candidate.camera_id)
                    job=HistoryImport.objects.select_for_update().get(pk=candidate.pk)
                    if job.status in ('failed','cancelled') and not HistoryImport.objects.filter(camera=camera,status__in=['pending','running']).exists():
                        # Re-scan the retained buffer, including records an old
                        # timestamp search may have skipped. Readings remain deduplicated.
                        job.status='pending';job.initialized=False;job.offset=0
                        job.scanned=0;job.imported=0;job.last_timestamp=None
                        job.first_offset=None;job.target_offset=None;job.snapshot_at=None
                        job.earliest_available=None;job.error='';job.recovery_mode='full';job.checkpoint={};job.end_anchor={};job.rate_at=None;job.rate_per_second=None;job.save()
                        AuditLog.objects.create(actor=request.user.get_username(),action='history_retry',object_type='HistoryImport',object_id=str(job.pk),changes={})
            return redirect('/imports/')
        if request.POST.get('action')=='cancel':
            with transaction.atomic():
                job=HistoryImport.objects.select_for_update().filter(pk=request.POST.get('job_id')).first()
                if job and job.status in ('pending','running'):
                    job.status='cancelled';job.save(update_fields=['status','updated_at'])
                    AuditLog.objects.create(actor=request.user.get_username(),action='history_cancel',object_type='HistoryImport',object_id=str(job.pk),changes={})
            return redirect('/imports/')
        form=ImportForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                camera=Camera.objects.select_for_update().get(pk=form.cleaned_data['camera'].pk)
                if HistoryImport.objects.filter(camera=camera,status__in=['pending','running','paused']).exists():form.add_error(None,'Este dispositivo já tem uma importação em andamento.')
                else:
                    job=form.save()
                    AuditLog.objects.create(actor=request.user.get_username(),action='history_import',object_type='HistoryImport',object_id=str(job.pk),changes={'camera':camera.pk,'start':str(job.start),'end':str(job.end)})
                    return redirect('/imports/')
    jobs=HistoryImport.objects.exclude(status='archived').select_related('camera').order_by('-id')[:50]
    from .import_cleanup import preview
    return render(request,'imports.html',{'form':form,'jobs':jobs,'cleanup_preview':preview() if request.user.is_superuser else None,'archives':ImportCleanup.objects.only('id','created_at','restored_at').filter(restored_at__isnull=True).order_by('-id')[:10] if request.user.is_superuser else []})

@login_required
@user_passes_test(lambda u:u.is_staff)
def progress(request):
    jobs=HistoryImport.objects.exclude(status='archived').order_by('-id')[:50]
    response=JsonResponse({'jobs':[{'id':j.pk,'mode':j.recovery_label,'fallback_reason':j.fallback_reason,'eta_seconds':j.eta_seconds,'rate':j.rate_per_second,'status':j.status,'progress':j.progress,'scanned':j.scanned,'imported':j.imported,'error':j.error,'updated_at':j.updated_at.isoformat(),'last_timestamp':j.last_timestamp.isoformat() if j.last_timestamp else None,'earliest_available':j.earliest_available.isoformat() if j.earliest_available else None} for j in jobs]})
    response['Cache-Control']='no-store'
    return response
