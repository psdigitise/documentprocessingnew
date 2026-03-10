from django.urls import re_path
from apps.processing import consumers

websocket_urlpatterns = [
    re_path(r'ws/workspace/(?P<doc_ref>[^/.]+)/(?P<page_number>\d+)/$', consumers.WorkspaceConsumer.as_asgi()),
    re_path(r'ws/notifications/$', consumers.NotificationConsumer.as_asgi()),
]
