from datetime import date,timedelta
from django.contrib import messages
from django.contrib.auth.decorators import login_required,user_passes_test
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import render,redirect
from django.utils import timezone
from .models import CollectionGap,ReportRevision,BackupRun,AuditLog
from .services import ZONE
from .intelligence import day_check,comparable_analysis,flow_month,weekly_executive

def selected_day(request):
    today=timezone.now().astimezone(ZONE).date()
    day=date.fromisoformat(request.GET.get('date',str(today)))
    if day.year<2000 or day>today:raise ValueError()
    return day

@login_required
@user_passes_test(lambda u:u.is_staff)
def checks(request):
    try:day=selected_day(request)
    except ValueError:return HttpResponseBadRequest('Escolha uma data de 2000 até hoje.')
    if request.method=='POST':
        if day!=timezone.now().astimezone(ZONE).date():messages.error(request,'A conferência direta dos equipamentos é disponível apenas para hoje. Consulte o reexame histórico nos demais dias.')
        else:
            from .verification import verified_total,VerificationError
            try:
                total=verified_total(day)
                messages.success(request,f'Conferência direta concluída: {total} entradas no corte indicado. Nenhuma mensagem enviada.')
            except VerificationError as error:messages.error(request,str(error))
            except Exception:messages.error(request,'Conferência não concluída. Verifique comunicação e importações.')
        return redirect('/checks/?date='+str(day))
    response=render(request,'checks.html',{'check':day_check(day),'day':day,'gaps':CollectionGap.objects.select_related('camera','job').order_by('-id')[:50],'revisions':ReportRevision.objects.filter(day=day).order_by('-id')[:50]})
    response['Cache-Control']='private, no-store';return response

@login_required
def heatmap(request):
    try:day=selected_day(request);data=flow_month(day)
    except (ValueError,OverflowError):return HttpResponseBadRequest('Data inválida.')
    return render(request,'flow_calendar.html',{'day':day,'data':data})

@login_required
def comparisons(request):
    try:day=selected_day(request);data=comparable_analysis(day)
    except (ValueError,OverflowError):return HttpResponseBadRequest('Data inválida.')
    return render(request,'comparisons.html',{'day':day,'data':data})

@login_required
def executive(request):
    try:day=selected_day(request);start=day-timedelta(days=day.weekday())
    except (ValueError,OverflowError):return HttpResponseBadRequest('Data inválida.')
    data=weekly_executive(start)
    return render(request,'executive.html',{'data':data,'start':start,'end':start+timedelta(days=6)})

@login_required
@user_passes_test(lambda u:u.is_superuser)
def backups(request):
    if request.method=='POST':
        with transaction.atomic():
            # Serialize requests against an existing admin row, including the empty queue.
            from django.contrib.auth import get_user_model
            from django.db import connection
            if connection.vendor=='postgresql':
                with connection.cursor() as cursor:cursor.execute('SELECT pg_advisory_xact_lock(77816,3)')
            get_user_model().objects.select_for_update().get(pk=request.user.pk)
            if BackupRun.objects.filter(status__in=['pending','running']).exists():messages.info(request,'Já existe um backup aguardando ou em execução.')
            else:
                run=BackupRun.objects.create(actor=request.user.get_username())
                AuditLog.objects.create(actor=request.user.get_username(),action='backup_request',object_type='BackupRun',object_id=str(run.pk),changes={})
                messages.success(request,'Backup solicitado. O serviço fará a cópia e testará a restauração em um banco temporário.')
        return redirect('/backups/')
    return render(request,'backups.html',{'runs':BackupRun.objects.order_by('-id')[:30],'last_verified':BackupRun.objects.filter(status='verified',verified_at__isnull=False).order_by('-verified_at').first()})
