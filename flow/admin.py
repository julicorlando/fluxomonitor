from django.contrib import admin
from .models import Camera,ApiKey,RawDelivery,Reading,DailyReport,DeviceGroup
admin.site.site_header='Axion • Fluxo'
@admin.register(Camera)
class DeviceAdmin(admin.ModelAdmin):
    list_display=('name','ip','location','enabled','last_seen')
    search_fields=('name','ip','serial','location')
@admin.register(ApiKey)
class KeyAdmin(admin.ModelAdmin):
    exclude=('digest',)
    def has_add_permission(self,request):return False
@admin.register(RawDelivery)
class RawAdmin(admin.ModelAdmin):
    list_display=('id','camera','received_at','status')
    readonly_fields=('camera','received_at','payload','status')
    def has_add_permission(self,request):return False
admin.site.register(Reading)
admin.site.register(DailyReport)

admin.site.register(DeviceGroup)

from .models import OpeningHours,CalendarException,FlowEvent,AuditLog,DeviceAlert,ScheduledSummary
admin.site.register([OpeningHours,CalendarException,FlowEvent])
@admin.register(AuditLog)
class AuditAdmin(admin.ModelAdmin):
    list_display=('at','actor','action','object_type','object_id')
    def has_add_permission(self,r):return False
    def has_change_permission(self,r,obj=None):return False
    def has_delete_permission(self,r,obj=None):return False

from .models import NativeWindow
@admin.register(NativeWindow)
class NativeWindowAdmin(admin.ModelAdmin):
    list_display=('camera','start_time','end_time','version_time')
    def has_add_permission(self,r):return False
    def has_change_permission(self,r,obj=None):return False
    def has_delete_permission(self,r,obj=None):return False
