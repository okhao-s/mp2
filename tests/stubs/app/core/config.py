import os


class _Settings:
    TZ = "Asia/Shanghai"
    API_TOKEN = "test-token"
    PROXY = None
    PROXY_SERVER = None
    CONFIG_PATH = os.environ.get("MP_CONFIG_PATH", "/tmp/mp-test-config")
    SECURITY_IMAGE_DOMAINS = []


settings = _Settings()
