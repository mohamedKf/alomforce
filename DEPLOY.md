# Setting up a new customer

One Railway project per customer, one Cloudinary account per customer. Their
data and their files stay theirs, and nothing one yard does can reach another.

Budget half an afternoon. Most of it is waiting for things to build.

## 1. The project

Create a Railway project from the `alomforce` repository and add a **MySQL**
database to it. Railway sets `DATABASE_URL` itself.

**Turn on backups for the MySQL service before the yard enters anything.**
This is the only step on this page whose mistake cannot be undone. The
database is small (single-digit megabytes, most of which is the catalogue that
ships in the code), so it costs very little.

## 2. Their variables

| Variable | Where it comes from | If it is missing |
|---|---|---|
| `SECRET_KEY` | generate a fresh one per customer, never reuse | the server refuses to start |
| `DEBUG` | `False` | the server refuses to start |
| `RELAXED_AUTH` | `False` | the server refuses to start |
| `CLOUDINARY_CLOUD_NAME` / `_API_KEY` / `_API_SECRET` | their own Cloudinary account | **files are written to the container and destroyed on the next deploy** |
| `EMAIL_HOST` / `EMAIL_PORT` / `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` / `DEFAULT_FROM_EMAIL` | their mail account | invoices cannot be emailed, the accountant gets nothing, and nothing says so |
| `FIREBASE_CREDENTIALS` | the service-account JSON | phones are not woken for orders or deliveries |
| `MAPBOX_TOKEN` | optional | the delivery map falls back to a plainer one |
| `REGISTER_CODE` | optional | a manager cannot sign themselves up |
| `SENTRY_DSN` / `SENTRY_DSN_CLIENTS` | optional | you hear about failures by telephone |
| `LATEST_DESKTOP_VERSION` / `LATEST_DESKTOP_URL` / `LATEST_ANDROID_VERSION` / `LATEST_ANDROID_BUILD` / `LATEST_ANDROID_URL` | optional | the apps cannot tell them an update exists |

The first three are enforced: the start command runs `check --deploy`, so a
deployment carrying a testing-phase switch fails instead of quietly running
with no password rules. Everything else is reported by the Setup page inside
the app rather than blocking a deploy, because a fresh install legitimately
starts without them.

Anything in this table can also be typed into the app's Settings screen
instead. A variable wins over the stored value, so use variables for things
you set once and Settings for things the customer may change.

## 3. Deploy

The first deploy migrates an empty database and loads all five catalogues by
itself: Klil, Extal, Alubin, Schremer and Profal, about 9,000 profiles, every
series given a starting price per kilo. There is nothing to import by hand.

## 4. Hand it over

If the database is fresh, skip to the manager. If you cloned a project that
had data in it, empty it first:

    railway ssh "python manage.py new_customer --dry-run"

    railway ssh "python manage.py new_customer \
        --shop 'אלומיניום הגליל בע\"מ' --tax-id 514123456 \
        --manager-id 123456782 --manager-name 'יוסי דהן' \
        --manager-phone 050-1234567 --yes"

That clears every order, client, invoice, delivery, shift and account, keeps
the catalogues, sets up the business and creates their first manager with a
printed temporary password they are asked to change at first sign-in.

On a genuinely empty database, create the manager with `createsuperuser` and
set the business details in Settings instead.

## 5. Check it before you leave

Open the desktop app's **Setup** page. It lists what is still unconfigured and
what stops working because of it, worst first. Nothing blocking should remain.

    railway ssh "python manage.py check_sentry --status"

Then walk one order end to end in front of them: pick a profile, price it,
make a delivery note, sign it on the phone, send it over WhatsApp. It proves
the catalogue, the pricing, the phone, the storage and the sharing in one go.

## 6. The apps

Install from `~/Desktop/AlomForce-releases`:

- **Windows**: `AlomForce-Setup-<version>.exe`. It installs properly, so the
  next version upgrades it in place rather than leaving two copies. Unsigned
  for now, so Windows shows "More info" then "Run anyway" once.
- **Android**: the APK, signed with our own release key. Later versions
  install over it as long as the build number has gone up.
- **macOS**: the zip. Unsigned, so the first open is right-click then Open.

Each app asks for the server address on first run, which is the Railway public
URL with `/api` on the end. Set it once per machine; an upgrade does not clear
it.

## Afterwards

Publish the release variables from the table above so their apps can tell them
when a new version is out, and point the download links at wherever you host
the installers.
