# Cloud Run setup

The image is single-container and serves both FastAPI and the built Vite assets. The backend uses Python 3.12 and the frontend uses Node only in the build stage.

```powershell
$PROJECT_ID = "your-project-id"
$REGION = "us-central1"
$REPO = "$REGION-docker.pkg.dev/$PROJECT_ID/ata"
gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com `
  aiplatform.googleapis.com firestore.googleapis.com pubsub.googleapis.com cloudtasks.googleapis.com `
  storage.googleapis.com secretmanager.googleapis.com logging.googleapis.com
gcloud artifacts repositories create ata --repository-format=docker --location=$REGION
gcloud firestore databases create --database="(default)" --location=$REGION
gcloud storage buckets create "gs://synthetic-ata-media" --location=$REGION --uniform-bucket-level-access
gcloud pubsub topics create incident-events
gcloud tasks queues create incident-workflows --location=$REGION
gcloud iam service-accounts create no-more-buckets-runtime --display-name="No More Buckets Cloud Run"
$RUNTIME_SA = "no-more-buckets-runtime@$PROJECT_ID.iam.gserviceaccount.com"
foreach ($ROLE in @("roles/aiplatform.user", "roles/datastore.user", "roles/storage.objectAdmin", "roles/pubsub.publisher", "roles/cloudtasks.enqueuer", "roles/logging.logWriter", "roles/secretmanager.secretAccessor")) {
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:$RUNTIME_SA" --role=$ROLE
}
gcloud auth configure-docker "$REGION-docker.pkg.dev"
gcloud builds submit --tag "$REPO/no-more-buckets:latest" .

gcloud run deploy no-more-buckets `
  --image "$REPO/no-more-buckets:latest" `
  --region $REGION `
  --service-account $RUNTIME_SA `
  --allow-unauthenticated `
  --set-env-vars "APP_ENV=cloud,DEMO_MODE=true,FACTS_PROVIDER=gemini,STORAGE_BACKEND=firestore,MESSAGING_PROVIDER=telegram,TELEGRAM_BOT_USERNAME=$env:TELEGRAM_BOT_USERNAME,GEMINI_MODEL=gemini-3.5-flash,GOOGLE_GENAI_USE_VERTEXAI=false,CURRENCY=SGD,SPENDING_LIMIT_DEFAULT=250,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION,GCS_BUCKET=synthetic-ata-media,PUBLIC_BASE_URL=https://REPLACE_WITH_CLOUD_RUN_URL"
```

Store Telegram and Gemini secrets in Secret Manager and inject them as secret environment variables in the deployment rather than putting them in `.env`:

```powershell
gcloud secrets create telegram-bot-token --replication-policy=automatic
gcloud secrets create telegram-webhook-secret --replication-policy=automatic
gcloud secrets create gemini-api-key --replication-policy=automatic
gcloud secrets versions add telegram-bot-token --data-file="$env:TELEGRAM_BOT_TOKEN_FILE"
gcloud secrets versions add telegram-webhook-secret --data-file="$env:TELEGRAM_WEBHOOK_SECRET_FILE"
gcloud secrets versions add gemini-api-key --data-file="$env:GEMINI_API_KEY_FILE"
gcloud run services update no-more-buckets --region $REGION `
  --update-secrets TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest,GEMINI_API_KEY=gemini-api-key:latest
```

After deployment, set `TELEGRAM_BOT_USERNAME` in the deploy shell and run `python scripts/register_telegram_webhook.py --base-url https://SERVICE_URL`. The script calls `setWebhook`, then `getWebhookInfo`, and verifies the exact `/api/webhooks/telegram` URL. Each Telegram user must send `/start` before receiving bot messages. Use the README pairing command to create a one-time deep link for the synthetic Vendor B record; the resulting chat ID is persisted in Firestore. Seed the reference catalog with `PYTHONPATH=backend python -m app.seed_data` using an identity with Firestore write access. Startup seeding is create-if-missing so it does not overwrite paired chat IDs.

For a pre-existing project, the create commands can be replaced with the corresponding `describe` checks. Grant only the runtime service-account roles required by the selected adapters.

The deploy command above is intentionally explicit about `GEMINI_MODEL=gemini-3.5-flash`; it must not be replaced by an older model. Configure `GOOGLE_APPLICATION_CREDENTIALS` only through the runtime identity/ADC path. Use synthetic fixtures from the demo adapter for the submission. If Vertex AI is selected explicitly, set `GOOGLE_GENAI_USE_VERTEXAI=true` and keep the same exact model; do not add Gemma or another bonus model.
