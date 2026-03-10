
import time
import hmac
import hashlib
from django.conf import settings

def get_upload_path(instance, filename):
    """
    Generates a unique upload path for files.
    """
    ext = filename.split('.')[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    today = datetime.now()
    return os.path.join(
        f"{instance._meta.app_label}",
        f"{today.year}/{today.month}/{today.day}",
        filename
    )

class SigningService:
    """
    Industrial-grade utility for signing URLs and data.
    Ensures temporary, one-time access to sensitive resources.
    """
    
    SECRET_KEY = settings.SECRET_KEY.encode('utf-8')
    DEFAULT_EXPIRY = 60 # 60 seconds

    @staticmethod
    def sign_url(original_url, user_id, expiry=None):
        """
        Generates a signed URL with a temporary token.
        """
        if expiry is None:
            expiry = int(time.time()) + SigningService.DEFAULT_EXPIRY
            
        message = f"{original_url}:{user_id}:{expiry}"
        signature = hmac.new(
            SigningService.SECRET_KEY,
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        separator = '&' if '?' in original_url else '?'
        return f"{original_url}{separator}signature={signature}&expires={expiry}&u={user_id}"

    @staticmethod
    def verify_signature(url_path, signature, expiry, user_id):
        """
        Verifies the signature and expiry of a signed URL.
        """
        try:
            if int(time.time()) > int(expiry):
                return False
                
            message = f"{url_path}:{user_id}:{expiry}"
            expected_signature = hmac.new(
                SigningService.SECRET_KEY,
                message.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            return hmac.compare_digest(expected_signature, signature)
        except (ValueError, TypeError):
            return False
