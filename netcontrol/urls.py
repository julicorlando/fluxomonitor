from django.urls import path
from . import views
urlpatterns = [path('',views.dashboard,name='netcontrol-dashboard'),path('equipamentos/novo/',views.asset_create,name='netcontrol-asset-create'),path('topology.json',views.topology_json,name='netcontrol-topology')]
