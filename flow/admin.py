from django.contrib import admin
from .models import Camera,ApiKey,RawDelivery,Reading,DailyReport
admin.site.site_header='Axion • Fluxo'
admin.site.register(Camera)
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
