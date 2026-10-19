from django import forms
from django.contrib.auth.decorators import login_required,user_passes_test
from django.shortcuts import render,redirect
from django.http import JsonResponse,HttpResponse
from .models import OpeningHours,CalendarException,FlowEvent,AuditLog,DeviceAlert,ScheduledSummary
from .views import access
class HoursForm(forms.ModelForm):
    class Meta:
        model=OpeningHours
        fields=['weekday','closed','opens','closes']
        labels={'weekday':'Dia da semana','closed':'Fechado','opens':'Abre','closes':'Fecha'}
        widgets={'opens':forms.TimeInput(attrs={'type':'time'}),'closes':forms.TimeInput(attrs={'type':'time'})}
    def clean(self):
        d=super().clean()
        if d.get('opens') and d.get('closes') and d['opens']>=d['closes']:raise forms.ValidationError('Fechamento deve ser depois da abertura; jornada noturna não suportada.')
        return d
class ExceptionForm(forms.ModelForm):
    class Meta:
        model=CalendarException
        fields=['day','closed','is_holiday','description']
        widgets={'day':forms.DateInput(attrs={'type':'date'})}
class EventForm(forms.ModelForm):
    class Meta:
        model=FlowEvent
        fields=['title','day','group','notes']
        labels={'title':'Evento / campanha','day':'Data','group':'Grupo','notes':'Anotações'}
        widgets={'day':forms.DateInput(attrs={'type':'date'})}
@login_required
@user_passes_test(lambda u:u.is_staff)
def calendar(request):
    hours=HoursForm(prefix='hours');exception=ExceptionForm(prefix='exception')
    if request.method=='POST':
        if request.POST.get('kind')=='hours':
            raw=request.POST.get('hours-weekday');obj=OpeningHours.objects.filter(weekday=raw).first() if raw and raw.isdigit() else None
            hours=HoursForm(request.POST,prefix='hours',instance=obj)
            if hours.is_valid():hours.save();return redirect('/calendar/')
        else:
            raw_day=request.POST.get('exception-day')
            from django.utils.dateparse import parse_date
            parsed=parse_date(raw_day) if raw_day else None
            obj=CalendarException.objects.filter(day=parsed).first() if parsed else None
            exception=ExceptionForm(request.POST,prefix='exception',instance=obj)
            if exception.is_valid():exception.save();return redirect('/calendar/')
    return render(request,'calendar.html',{'hours_form':hours,'exception_form':exception,'hours':OpeningHours.objects.order_by('weekday'),'exceptions':CalendarException.objects.order_by('-day')[:100]})
@login_required
@user_passes_test(lambda u:u.is_staff)
def events(request):
    form=EventForm(request.POST or None)
    if request.method=='POST' and form.is_valid():form.save();return redirect('/events/')
    return render(request,'events.html',{'form':form,'events':FlowEvent.objects.order_by('-day')[:200]})
@login_required
@user_passes_test(lambda u:u.is_superuser)
def audit(request):return render(request,'audit.html',{'logs':AuditLog.objects.order_by('-id')[:200]})
@login_required
def alerts(request):
    from .notification_views import alert_context
    context=alert_context(request)
    if not isinstance(context,dict):return context
    if request.method=='POST' and not request.user.is_superuser:return HttpResponse('Sem permissão',status=403)
    response=render(request,'alerts.html',{'alerts':DeviceAlert.objects.select_related('camera').order_by('-opened_at')[:200],**context})
    response['Cache-Control']='private, no-store';return response
@login_required
def summaries(request):return render(request,'summaries.html',{'summaries':ScheduledSummary.objects.order_by('-end','kind')[:100]})
@login_required
def docs(request):return render(request,'api_docs.html')
@access('read')
def alerts_api(request):
    qs=DeviceAlert.objects.all()
    if request.api_key and request.api_key.camera_id:qs=qs.filter(camera_id=request.api_key.camera_id)
    return JsonResponse({'alerts':list(qs.order_by('-opened_at').values('id','camera_id','opened_at','resolved_at','reason')[:200])})
