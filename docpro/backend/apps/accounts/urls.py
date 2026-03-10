from django.urls import path, include
from rest_framework.routers import SimpleRouter
from apps.accounts.views import UserViewSet, ResourceViewSet

router = SimpleRouter()
router.register(r'users', UserViewSet)
router.register(r'resources', ResourceViewSet)

urlpatterns = [
    path('', include(router.urls)),
]
