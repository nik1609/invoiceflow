#!/usr/bin/env bash
# Deploy InvoiceFlow to Google Cloud Run. Run it from the project folder on your own computer:
#   gcloud auth login
#   PROJECT_ID=your-project-id bash deploy/gcp.sh
# It asks for the Groq key and, optionally, a shared password, stores them in Secret Manager (never in the image),
# and deploys one always-on instance.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID, for example: PROJECT_ID=my-project bash deploy/gcp.sh}"
REGION="${REGION:-asia-south1}"      # Mumbai
SERVICE="${SERVICE:-invoiceflow}"

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

secret() {   # name, prompt
  local name="$1" prompt="$2" value
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    read -r -p "Secret $name exists. Replace it? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || return 0
  else
    gcloud secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  read -r -s -p "$prompt: " value; echo
  printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
}
secret groq-api-key "Groq API key"
read -r -p "Protect the link with a shared password? [y/N] " usepw
SECRETS="GROQ_API_KEY=groq-api-key:latest"
SECRET_NAMES="groq-api-key"
if [[ "$usepw" =~ ^[Yy]$ ]]; then
  secret invoiceflow-password "Shared password for reviewers"
  SECRETS="$SECRETS,APP_PASSWORD=invoiceflow-password:latest"
  SECRET_NAMES="$SECRET_NAMES invoiceflow-password"
fi

NUM="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
for s in $SECRET_NAMES; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:${NUM}-compute@developer.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor >/dev/null
done

# One instance that stays warm (min 1) and never scales out (max 1), because runs are stored in a local SQLite file.
# CPU is only charged while a request is open. Live runs stream over an open request, so they keep running while
# someone watches. Add --no-cpu-throttling (about 4x the cost) if runs must finish with nobody watching.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --cpu 1 --memory 1Gi --min-instances 1 --max-instances 1 --concurrency 40 --timeout 3600 \
  --set-secrets "$SECRETS"

echo; echo "Live at:"; gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)'
[[ "$usepw" =~ ^[Yy]$ ]] && echo "Sign in with any username and the shared password."
echo "Set a budget alert in Billing before you share the link."
