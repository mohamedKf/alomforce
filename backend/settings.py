"""
Django settings for the AlomForce backend.

The backend is the only project that owns settings. `core` is a plain app
package holding the shared models and views; desktop and mobile are clients
that talk to this project over the REST API.
"""

from pathlib import Path

from decouple import Csv, config
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='django-insecure-dev-only-change-me')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())


# Application definition

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'corsheaders',
    'cloudinary',
    'cloudinary_storage',
    'rest_framework_simplejwt',
]

CORE_APPS = [
    'core',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + CORE_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'django.template.context_processors.i18n',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'
ASGI_APPLICATION = 'backend.asgi.application'


# Database
# SQLite for local development; point DATABASE_URL at Postgres for anything shared.

if config('DB_ENGINE', default='sqlite') == 'postgres':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': config('DB_NAME'),
            'USER': config('DB_USER'),
            'PASSWORD': config('DB_PASSWORD'),
            'HOST': config('DB_HOST', default='localhost'),
            'PORT': config('DB_PORT', default='5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'core.User'

# Testing-phase switch.
#
# RELAXED_AUTH=True drops the ID check-digit test and every password rule, so
# throwaway accounts like 12345678 / "12345678" work while the apps are being
# built. It defaults to False and a deploy check refuses to start with it on
# while DEBUG is off -- otherwise this is exactly the setting that quietly
# reaches production and leaves the stock system on guessable passwords.
RELAXED_AUTH = config('RELAXED_AUTH', default=False, cast=bool)

# The real rules, always defined so they can be applied at request time
# regardless of the flag. The API reads this list via PasswordRulesMixin
# rather than AUTH_PASSWORD_VALIDATORS, so toggling RELAXED_AUTH in a test
# changes behaviour without needing the derived setting recomputed.
STRICT_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# What Django itself uses (admin, createsuperuser). Emptied under RELAXED_AUTH
# so those paths accept throwaway passwords too during the testing phase.
AUTH_PASSWORD_VALIDATORS = [] if RELAXED_AUTH else STRICT_PASSWORD_VALIDATORS


# Internationalization
#
# The UI is authored in English; Hebrew and Arabic ship as .po translations and
# render RTL. Wrap user-facing strings in gettext rather than hand-rolling a
# translation table, so strings stay extractable with `makemessages`.

LANGUAGE_CODE = 'en'

LANGUAGES = [
    ('en', _('English')),
    ('he', _('Hebrew')),
    ('ar', _('Arabic')),
]

LOCALE_PATHS = [BASE_DIR / 'locale']

TIME_ZONE = 'Asia/Jerusalem'
USE_I18N = True
USE_TZ = True


# Static and media
#
# Cloudinary holds profile section drawings, document PDFs and delivery photos.

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

CLOUDINARY_STORAGE = {
    'CLOUD_NAME': config('CLOUDINARY_CLOUD_NAME', default=''),
    'API_KEY': config('CLOUDINARY_API_KEY', default=''),
    'API_SECRET': config('CLOUDINARY_API_SECRET', default=''),
}

STORAGES = {
    'default': {
        'BACKEND': 'cloudinary_storage.storage.MediaCloudinaryStorage'
        if config('CLOUDINARY_CLOUD_NAME', default='')
        else 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Mapbox public token for the desktop map. A public (pk.) token is meant to be
# embedded in client code; kept here so it lives in one place and out of source.
MAPBOX_TOKEN = config('MAPBOX_TOKEN', default='')


# API

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'core.authentication.PresenceJWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
        # Applied globally so a manager-set starting password cannot be used
        # for anything except replacing itself.
        'core.permissions.PasswordChangeRequired',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
}

CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:5173,http://localhost:3000',
    cast=Csv(),
)

# In development, allow any origin so a Flutter *web* build (served on an
# arbitrary localhost port) can call the API from a browser while testing. Off
# in production, where only the configured origins are allowed. Native phone
# builds don't enforce CORS, so this only affects browser-based dev testing.
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
