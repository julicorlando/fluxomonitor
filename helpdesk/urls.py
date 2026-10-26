from django.urls import path
from . import views, suite_views as suite

urlpatterns = [
    path('', views.home),
    path('assets/',suite.assets),
    path('assets/new/',suite.asset_detail),
    path('assets/<int:asset_id>/',suite.asset_detail),
    path('knowledge/',suite.knowledge),
    path('knowledge/new/',suite.knowledge_detail),
    path('knowledge/<int:article_id>/',suite.knowledge_detail),
    path('problems/',suite.problems),
    path('problems/new/',suite.problem_detail),
    path('problems/<int:problem_id>/',suite.problem_detail),
    path('sla/company/<str:code>/',suite.company_sla),
    path('sla/agents/',suite.agents),
    path('sla/agents/<int:profile_id>/',suite.agents),
    path('sla/alerts/',suite.alerts),
    path('sla/', views.sla_settings),
    path('contacts/', views.contacts),
    path('contacts/<int:contact_id>/', views.contacts),
    path('settings/', views.settings),
    path('messages/', views.deliveries),
    path('inbox/', views.master_inbox),
    path('inbox/<int:contact_id>/', views.master_inbox),
    path('pulse/', views.pulse),
    path('tickets/<int:ticket_id>/', views.detail),
    path('webhook/', views.webhook),
]
