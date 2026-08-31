# Google Cloud and Telegram setup

This guide deploys one Cloud Run service for the FastAPI backend and React application. It uses Firestore for state, Cloud Storage for media, Cloud Tasks for deadlines, Pub/Sub for event envelopes, Secret Manager for credentials, and the Gemini API for Gemini 3.5 Flash.

All examples use PowerShell. Never place secret values in the repository.

If you only want the deterministic local demo, use the Docker instructions in the root README and stop there. The local demo requires no Google Cloud, Gemini, or Telegram credentials.

## 1. Create your accounts and credentials

1. Install [Python 3.12](https://www.python.org/downloads/) and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install-sdk).
2. Create a [Google Cloud project](https://console.cloud.google.com/projectcreate). Project IDs are globally unique, so choose your own value.
3. [Enable billing for that project](https://cloud.google.com/billing/docs/how-to/modify-project). Cloud Run and the other managed services cannot be deployed without an active billing account.
4. Open the [Google AI Studio API Keys page](https://aistudio.google.com/app/apikey), select or import your project, and create a Gemini API key. Copy it into a temporary text file outside this repository.
5. In Telegram, open the verified [BotFather](https://t.me/BotFather), send `/newbot`, and choose a display name and a username ending in `bot`. Copy the token into a second temporary text file outside this repository. Record the username separately without the leading `@`.
6. Generate the Telegram webhook secret with the command in step 5 and place it in a third temporary text file.
7. Authenticate the CLI and confirm the correct account before creating resources:

~~~powershell
gcloud auth login
gcloud auth list
~~~

Do not put any of these three secret values in `.env`, `.env.example`, a command committed to shell history, source code, screenshots, or GitHub. The deployment reads them from Secret Manager.

## 2. Select names

~~~powershell
$PROJECT_ID = "your-project-id"
$REGION = "us-central1"
$SERVICE = "no-more-buckets"
$AR_REPOSITORY = "ata"
$BUCKET = "$PROJECT_ID-no-more-buckets-media"
$RUNTIME_SA_NAME = "no-more-buckets-runtime"
$RUNTIME_SA = "$RUNTIME_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
$IMAGE = "$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPOSITORY/no-more-buckets:latest"
gcloud config set project $PROJECT_ID
~~~

## 3. Enable APIs

~~~powershell
$APIS = @("run.googleapis.com","artifactregistry.googleapis.com","cloudbuild.googleapis.com","firestore.googleapis.com","storage.googleapis.com","pubsub.googleapis.com","cloudtasks.googleapis.com","secretmanager.googleapis.com","logging.googleapis.com","iam.googleapis.com")
gcloud services enable $APIS
~~~

## 4. Create resources

Run each create command once. If a resource already exists, use its corresponding describe command instead.

~~~powershell
gcloud artifacts repositories create $AR_REPOSITORY --repository-format=docker --location=$REGION
gcloud firestore databases create --database="(default)" --location=$REGION --type=firestore-native
gcloud storage buckets create "gs://$BUCKET" --location=$REGION --uniform-bucket-level-access
gcloud pubsub topics create incident-events
gcloud tasks queues create incident-workflows --location=$REGION
gcloud iam service-accounts create $RUNTIME_SA_NAME --display-name="No More Buckets Cloud Run"
~~~

Grant only the roles used by the selected adapters:

~~~powershell
$ROLES = @("roles/datastore.user","roles/storage.objectAdmin","roles/pubsub.publisher","roles/cloudtasks.enqueuer","roles/logging.logWriter","roles/secretmanager.secretAccessor")
foreach ($ROLE in $ROLES) {
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:$RUNTIME_SA" --role=$ROLE
}
~~~

## 5. Generate and store secrets

Create three temporary text files outside the repository. Each file must contain only one value: the Telegram bot token, Telegram webhook secret, or Gemini API key.

Generate the webhook secret first and save the printed value in its temporary file:

~~~powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
~~~

Then create the Secret Manager entries and upload one value to each:

~~~powershell
gcloud secrets create telegram-bot-token --replication-policy=automatic
gcloud secrets create telegram-webhook-secret --replication-policy=automatic
gcloud secrets create gemini-api-key --replication-policy=automatic
gcloud secrets versions add telegram-bot-token --data-file="PATH_TO_BOT_TOKEN_FILE"
gcloud secrets versions add telegram-webhook-secret --data-file="PATH_TO_WEBHOOK_SECRET_FILE"
gcloud secrets versions add gemini-api-key --data-file="PATH_TO_GEMINI_KEY_FILE"
~~~

After all three `gcloud secrets versions add` commands succeed, permanently delete the temporary files.

## 6. Build the container

~~~powershell
gcloud builds submit --tag $IMAGE .
~~~

The root Dockerfile builds the frontend with Node 22 and serves the API and static frontend from one Python 3.12 container.

## 7. Bootstrap Cloud Run

The Cloud Tasks adapter needs the final public service URL. First deploy with local adapters so Cloud Run creates that URL:

~~~powershell
gcloud run deploy $SERVICE --image $IMAGE --region $REGION --service-account $RUNTIME_SA --allow-unauthenticated --set-env-vars "APP_ENV=cloud,DEMO_MODE=true,ADK_ENABLED=false,FACTS_PROVIDER=deterministic,STORAGE_BACKEND=memory,MESSAGING_PROVIDER=local,GEMINI_MODEL=gemini-3.5-flash"
$SERVICE_URL = (gcloud run services describe $SERVICE --region $REGION --format="value(status.url)").Trim()
$SERVICE_URL
~~~

## 8. Enable cloud adapters

Set the BotFather username without the leading at sign:

~~~powershell
$TELEGRAM_BOT_USERNAME = "your_bot_username"
gcloud run services update $SERVICE --region $REGION --set-env-vars "APP_ENV=cloud,DEMO_MODE=true,ADK_ENABLED=true,FACTS_PROVIDER=gemini,STORAGE_BACKEND=firestore,MESSAGING_PROVIDER=telegram,TELEGRAM_BOT_USERNAME=$TELEGRAM_BOT_USERNAME,TELEGRAM_DRAFT_EXPIRY_SECONDS=900,GEMINI_MODEL=gemini-3.5-flash,GOOGLE_GENAI_USE_VERTEXAI=false,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,FIRESTORE_DATABASE=(default),GCS_BUCKET=$BUCKET,PUBSUB_TOPIC=incident-events,TASKS_QUEUE=incident-workflows,PUBLIC_BASE_URL=$SERVICE_URL,CURRENCY=SGD,SPENDING_LIMIT_DEFAULT=250,HUMAN_VENDOR_TIMEOUT_SECONDS=600,DISPLAY_TIMEZONE=Asia/Singapore" --update-secrets "TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest,GEMINI_API_KEY=gemini-api-key:latest"
~~~

Startup creates the synthetic reference catalog only when records are missing. Existing Telegram pairings are not overwritten.

## 9. Register the Telegram webhook

Load the secrets into the current process without printing them:

~~~powershell
$env:TELEGRAM_BOT_TOKEN = (gcloud secrets versions access latest --secret=telegram-bot-token --project=$PROJECT_ID).Trim()
$env:TELEGRAM_WEBHOOK_SECRET = (gcloud secrets versions access latest --secret=telegram-webhook-secret --project=$PROJECT_ID).Trim()
$env:TELEGRAM_BOT_USERNAME = $TELEGRAM_BOT_USERNAME
$env:PYTHONPATH = "backend"
python scripts/register_telegram_webhook.py --base-url $SERVICE_URL
~~~

The helper registers and verifies the exact webhook URL without printing the token or webhook secret.

## 10. Pair the tenant and Vendor B

~~~powershell
$TENANT_PAIRING = Invoke-RestMethod -Method Post -Uri "$SERVICE_URL/api/telegram/pairing-codes" -ContentType "application/json" -Body '{"target_type":"tenant","target_id":"tenant-demo-001"}'
$TENANT_PAIRING.deep_link
~~~

Open the tenant deep link in the tenant's private Telegram chat.

~~~powershell
$VENDOR_PAIRING = Invoke-RestMethod -Method Post -Uri "$SERVICE_URL/api/telegram/pairing-codes" -ContentType "application/json" -Body '{"target_type":"vendor","target_id":"vendor-b"}'
$VENDOR_PAIRING.code
~~~

In the intended Vendor B Telegram group, send:

~~~text
/start PAIRING_CODE
~~~

Each code expires after 15 minutes and can be used once. Both the tenant chat and vendor group should send /start once more before a demo so outbound delivery is marked ready.

## 11. Verify deployment

~~~powershell
Invoke-RestMethod "$SERVICE_URL/api/health"
Invoke-RestMethod "$SERVICE_URL/api/runtime"
Invoke-RestMethod "$SERVICE_URL/api/incidents"
gcloud tasks queues describe incident-workflows --location=$REGION --project=$PROJECT_ID
gcloud run services describe $SERVICE --region=$REGION --format="value(status.url,status.latestReadyRevisionName)"
~~~

The runtime response should report Cloud Run, Gemini API, gemini-3.5-flash, Firestore, and Telegram.

## Optional exact-model check

~~~powershell
$env:GEMINI_API_KEY = (gcloud secrets versions access latest --secret=gemini-api-key --project=$PROJECT_ID).Trim()
$env:GEMINI_MODEL = "gemini-3.5-flash"
$env:PYTHONPATH = "backend"
python -m app.gemini_smoke
~~~

The command reports only success or a redacted error and never falls back to an older model.
