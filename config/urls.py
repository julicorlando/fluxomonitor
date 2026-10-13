from django.urls import path
from django.contrib import admin
from django.contrib.auth import views as auth
from flow import views
urlpatterns=[path('',views.dashboard),path('admin/',admin.site.urls),path('login/',auth.LoginView.as_view(template_name='login.html')),path('logout/',auth.LogoutView.as_view(next_page='/login/')),path('health/',views.health),path('api/v1/live/',views.live),path('api/v1/cameras/',views.cameras),path('api/v1/history/',views.history),path('api/v1/export.csv',views.export),path('api/v1/ingest/',views.ingest),path('api/camera/dataUpload',views.ingest),path('api/camera/heartBeat',views.ingest,{'heartbeat':True})]
