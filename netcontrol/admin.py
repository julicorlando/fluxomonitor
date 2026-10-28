from django.contrib import admin
from .models import Site, NetworkAsset, Interface, NetworkLink, PollHistory

@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ('name','description')

@admin.register(NetworkAsset)
class NetworkAssetAdmin(admin.ModelAdmin):
    list_display = ('name','kind','ip','site','manufacturer','model','last_status','last_checked')
    list_filter = ('kind','site','protocol','enabled','last_status')
    search_fields = ('name','ip','mac','location','patrimony')
    readonly_fields = ('last_status','last_checked','last_error')

@admin.register(Interface)
class InterfaceAdmin(admin.ModelAdmin):
    list_display = ('asset','index','name','status','vlan','speed_bps')
    list_filter = ('status',)
    search_fields = ('asset__name','name')

@admin.register(NetworkLink)
class NetworkLinkAdmin(admin.ModelAdmin):
    list_display = ('source','target','source_interface','evidence')

@admin.register(PollHistory)
class PollHistoryAdmin(admin.ModelAdmin):
    list_display = ('asset','created_at','successful','detail')
    readonly_fields = ('asset','created_at','successful','detail')
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
