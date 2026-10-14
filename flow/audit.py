from contextvars import ContextVar
from django.db.models.signals import pre_save,post_save,post_delete
from django.dispatch import receiver
from django.contrib.auth.models import User,Group
from .models import Camera,ApiKey,DeviceGroup,FlowEvent,OpeningHours,CalendarException,AuditLog,WhatsAppConfig
actor=ContextVar('audit_actor',default='sistema')
class AuditMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        token=actor.set(request.user.username if request.user.is_authenticated else 'anônimo')
        try:return self.get_response(request)
        finally:actor.reset(token)
TRACKED=(Camera,ApiKey,DeviceGroup,FlowEvent,OpeningHours,CalendarException,User,WhatsAppConfig)
def values(obj):
    return {f.attname:str(getattr(obj,f.attname)) for f in obj._meta.concrete_fields if f.name not in ('password','digest','last_seen','last_login','api_url')}
@receiver(pre_save)
def before(sender,instance,**kwargs):
    if sender in TRACKED:
        old=sender.objects.filter(pk=instance.pk).first() if instance.pk else None
        instance._audit_previous=values(old) if old else {}
@receiver(post_save)
def after(sender,instance,created,**kwargs):
    if sender not in TRACKED:return
    old=getattr(instance,'_audit_previous',{});new=values(instance)
    changes={k:{'before':old.get(k),'after':v} for k,v in new.items() if old.get(k)!=v}
    if changes:AuditLog.objects.create(actor=actor.get(),action='create' if created else 'update',object_type=sender.__name__,object_id=str(instance.pk),changes=changes)
@receiver(post_delete)
def deleted(sender,instance,**kwargs):
    if sender in TRACKED:AuditLog.objects.create(actor=actor.get(),action='delete',object_type=sender.__name__,object_id=str(instance.pk))

from django.db.models.signals import m2m_changed
@receiver(m2m_changed)
def memberships(sender,instance,action,pk_set,**kwargs):
    if sender not in (User.groups.through,User.user_permissions.through,Group.permissions.through):return
    if action in ('post_add','post_remove','post_clear'):
        AuditLog.objects.create(actor=actor.get(),action=action,object_type=instance.__class__.__name__,object_id=str(instance.pk),changes={'relationship':sender.__name__,'ids':sorted(pk_set or [])})
