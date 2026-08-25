"""Production adapter implementations. Imports stay lazy so local/demo mode is credential-free."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .models import MediaAsset


class GoogleCloudStorageMediaStore:
    provider_name = "cloud_storage"

    def __init__(self, settings: Settings) -> None:
        from google.cloud import storage  # type: ignore[attr-defined]

        if not settings.gcs_bucket:
            raise ValueError("GCS_BUCKET is required for cloud storage")
        self.bucket = storage.Client(project=settings.google_cloud_project).bucket(
            settings.gcs_bucket
        )

    def put(self, asset: MediaAsset) -> str:
        blob = self.bucket.blob(f"incidents/{asset.asset_id}/{asset.filename}")
        if asset.content_base64:
            blob.upload_from_string(
                base64.b64decode(asset.content_base64), content_type=asset.mime_type
            )
        return f"gs://{self.bucket.name}/{blob.name}"


class GoogleSecretManagerProvider:
    provider_name = "secret_manager"

    def __init__(self, settings: Settings) -> None:
        from google.cloud import secretmanager  # type: ignore[attr-defined]

        if not settings.google_cloud_project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required for Secret Manager")
        self.client = secretmanager.SecretManagerServiceClient()
        self.project = settings.google_cloud_project

    def get(self, secret_id: str, version: str = "latest") -> str:
        name = f"projects/{self.project}/secrets/{secret_id}/versions/{version}"
        response = self.client.access_secret_version(request={"name": name})
        return response.payload.data.decode("utf-8")


class GooglePubSubEventBus:
    provider_name = "pubsub"

    def __init__(self, settings: Settings) -> None:
        from google.cloud import pubsub_v1  # type: ignore[attr-defined]

        if not settings.google_cloud_project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required for Pub/Sub")
        self.publisher = pubsub_v1.PublisherClient()
        self.topic_path = self.publisher.topic_path(
            settings.google_cloud_project, settings.pubsub_topic
        )

    def publish(
        self, event_id: str, incident_id: str, event_type: str, payload: dict[str, Any]
    ) -> str:
        body = json.dumps(
            {
                "event_id": event_id,
                "incident_id": incident_id,
                "event_type": event_type,
                "payload": payload,
            }
        ).encode()
        future = self.publisher.publish(
            self.topic_path, body, event_id=event_id, incident_id=incident_id
        )
        return str(future.result())


class GoogleCloudTasksQueue:
    provider_name = "cloud_tasks"

    def __init__(self, settings: Settings) -> None:
        from google.cloud import tasks_v2

        if not settings.google_cloud_project:
            raise ValueError("GOOGLE_CLOUD_PROJECT is required for Cloud Tasks")
        self.client = tasks_v2.CloudTasksClient()
        self.project = settings.google_cloud_project
        self.location = settings.google_cloud_location
        self.queue = settings.tasks_queue
        if not settings.public_base_url:
            raise ValueError("PUBLIC_BASE_URL is required for Cloud Tasks HTTP delivery")
        parsed = urlparse(settings.public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("PUBLIC_BASE_URL must be an absolute HTTP(S) URL")
        self.settings = settings
        self.target_url = f"{settings.public_base_url.rstrip('/')}/api/events/tasks"

    def enqueue(
        self,
        task_id: str,
        incident_id: str,
        task_type: str,
        payload: dict[str, Any],
        delay_seconds: int,
    ) -> str:
        from google.protobuf import timestamp_pb2  # type: ignore[import-untyped]

        parent = self.client.queue_path(self.project, self.location, self.queue)
        timestamp = timestamp_pb2.Timestamp()
        timestamp.FromDatetime(datetime.now(UTC) + timedelta(seconds=delay_seconds))
        task = {
            "name": f"{parent}/tasks/{task_id}",
            "schedule_time": timestamp,
            "http_request": {
                "http_method": "POST",
                "url": self.target_url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {
                        "task_id": task_id,
                        "incident_id": incident_id,
                        "task_type": task_type,
                        "payload": payload,
                    }
                ).encode(),
            },
        }
        if self.settings.cloud_tasks_invoker_service_account:
            task["http_request"]["oidc_token"] = {
                "service_account_email": self.settings.cloud_tasks_invoker_service_account,
                "audience": self.settings.public_base_url,
            }
        return self.client.create_task(parent=parent, task=task).name  # type: ignore[arg-type]
