from django.urls import path
from . import views

urlpatterns = [
    path('', views.home),
    path('contacts/', views.contacts),
    path('contacts/<int:contact_id>/', views.contacts),
    path('settings/', views.settings),
    path('messages/', views.deliveries),
    path('pulse/', views.pulse),
    path('tickets/<int:ticket_id>/', views.detail),
    path('webhook/', views.webhook),
]
