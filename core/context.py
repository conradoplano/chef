from django.conf import settings


def site(request):
    return {"site_name": settings.SITE_NAME, "dev_login": settings.DEV_LOGIN, "registration_open": settings.REGISTRATION_OPEN}
