from django.urls import path, include
from rest_framework.routers import SimpleRouter
from apps.documents.views import (
    DocumentViewSet, PageViewSet, BlockUpdateView,
    ConversionRetryView, ConversionStatusView
)

router = SimpleRouter()
router.register(r'pages', PageViewSet, basename='page')
router.register(r'', DocumentViewSet, basename='document')

urlpatterns = [
    path('blocks/<str:block_id>/', BlockUpdateView.as_view(), name='block-update'),
    path('conversion-status/<uuid:document_id>/', ConversionStatusView.as_view(), name='conversion-status'),
    path('<uuid:document_id>/retry-conversion/', ConversionRetryView.as_view(), name='conversion-retry'),
    path('', include(router.urls)),
]
