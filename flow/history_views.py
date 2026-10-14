import os
from datetime import timedelta
from django import forms
from django.contrib.auth.decorators import login_required,user_passes_test
from django.shortcuts import render,redirect
from django.db import transaction
from django.utils import timezone
from .models import Camera,HistoryImport,AuditLog
from .services import ZONE

class ImportForm(forms.ModelForm):
    class Meta:
        model=HistoryImport
        fields=['camera','start','end']
        labels={'camera':'Dispositivo','start':'Data inicial','end':'Data final'}
        widgets={'start':forms.DateInput(attrs={'type':'date'}),'end':forms.DateInput(attrs={'type':'date'})}
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
                if HistoryImport.objects.filter(camera=camera,status__in=['pending','running']).exists():form.add_error(None,'Este dispositivo já tem uma importação em andamento.')
                else:
                    job=form.save()
                    AuditLog.objects.create(actor=request.user.get_username(),action='history_import',object_type='HistoryImport',object_id=str(job.pk),changes={'camera':camera.pk,'start':str(job.start),'end':str(job.end)})
                    return redirect('/imports/')
    jobs=HistoryImport.objects.select_related('camera').order_by('-id')[:50]
    return render(request,'imports.html',{'form':form,'jobs':jobs})
