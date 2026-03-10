from django.urls import re_path
from apps.documents import consumers

websocket_urlpatterns = [
    re_path(
        r'ws/conversion/(?P<document_id>[^/]+)/$',
        consumers.ConversionStatusConsumer.as_asgi()
    ),
]
