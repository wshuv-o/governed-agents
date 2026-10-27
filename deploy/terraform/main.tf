terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  apis = ["run.googleapis.com", "sqladmin.googleapis.com", "secretmanager.googleapis.com",
  "artifactregistry.googleapis.com", "iamcredentials.googleapis.com", "sts.googleapis.com"]
  image = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}/governed-agents:${var.image_tag}"
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  repository_id = "agents"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]

  docker_config {
    immutable_tags = true
  }
}

# --- Database: the queue, checkpoints and audit log live here ---

resource "google_sql_database_instance" "pg" {
  name                = "governed-agents"
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection
  depends_on          = [google_project_service.apis]

  settings {
    tier              = var.db_tier
    edition           = "ENTERPRISE"
    availability_type = var.db_high_availability ? "REGIONAL" : "ZONAL"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      ipv4_enabled = true # reached only through the Cloud SQL connector (IAM-authorised); no authorised networks
      ssl_mode     = "ENCRYPTED_ONLY"
    }

    database_flags {
      name  = "log_min_duration_statement"
      value = "500"
    }
  }
}

resource "google_sql_database" "agents" {
  name     = "agents"
  instance = google_sql_database_instance.pg.name
}

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "agents" {
  name     = "agents"
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# --- Secrets. Values the platform generates are written here; the model key is added by a human. ---

resource "random_password" "webhook" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "s" {
  for_each   = toset(["database-url", "webhook-secret", "operator-keys", "model-api-key"])
  secret_id  = "agents-${each.value}"
  depends_on = [google_project_service.apis]

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "database_url" {
  secret      = google_secret_manager_secret.s["database-url"].id
  secret_data = "postgresql://agents:${random_password.db.result}@/agents?host=/cloudsql/${google_sql_database_instance.pg.connection_name}"
}

resource "google_secret_manager_secret_version" "webhook" {
  secret      = google_secret_manager_secret.s["webhook-secret"].id
  secret_data = random_password.webhook.result
}

# --- One identity per service, each with only what it uses ---

resource "google_service_account" "api" {
  account_id   = "agents-api"
  display_name = "governed-agents API"
}

resource "google_service_account" "worker" {
  account_id   = "agents-worker"
  display_name = "governed-agents worker"
}

resource "google_project_iam_member" "sql_client" {
  for_each = { api = google_service_account.api.email, worker = google_service_account.worker.email }
  project  = var.project_id
  role     = "roles/cloudsql.client"
  member   = "serviceAccount:${each.value}"
}

locals {
  # The API never sees the model key; the worker never sees webhook or operator secrets.
  secret_grants = {
    "api-database-url"   = { sa = google_service_account.api.email, secret = "database-url" }
    "api-webhook-secret" = { sa = google_service_account.api.email, secret = "webhook-secret" }
    "api-operator-keys"  = { sa = google_service_account.api.email, secret = "operator-keys" }
    "wrk-database-url"   = { sa = google_service_account.worker.email, secret = "database-url" }
    "wrk-model-api-key"  = { sa = google_service_account.worker.email, secret = "model-api-key" }
  }
}

resource "google_secret_manager_secret_iam_member" "grants" {
  for_each  = local.secret_grants
  secret_id = google_secret_manager_secret.s[each.value.secret].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.sa}"
}

# --- Services ---

resource "google_cloud_run_v2_service" "api" {
  name                = "agents-api"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL" # webhooks arrive from outside; every route checks a signature or key
  deletion_protection = false

  template {
    service_account = google_service_account.api.email
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.pg.connection_name]
      }
    }
    containers {
      image = local.image
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      dynamic "env" {
        for_each = { DATABASE_URL = "database-url", WEBHOOK_SECRET = "webhook-secret", OPERATOR_KEYS = "operator-keys" }
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.s[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.grants]

  lifecycle {
    ignore_changes = [template[0].containers[0].image] # CI rolls out new images (deploy.yml)
  }
}

resource "google_cloud_run_v2_service" "worker" {
  name                = "agents-worker"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"
  deletion_protection = false

  template {
    service_account = google_service_account.worker.email
    scaling {
      min_instance_count = 1 # always polling the queue
      max_instance_count = var.max_workers
    }
    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.pg.connection_name]
      }
    }
    containers {
      image   = local.image
      command = ["python", "-m", "governed_agents.worker"]
      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = false # keep CPU allocated between requests: the worker does background work
      }
      env {
        name  = "MODEL_PROVIDER"
        value = var.model_provider
      }
      env {
        name  = "MODEL_NAME"
        value = var.model_name
      }
      env {
        name = "DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.s["database-url"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = var.model_provider == "anthropic" ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.s["model-api-key"].secret_id
            version = "latest"
          }
        }
      }
      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
      liveness_probe {
        http_get {
          path = "/healthz" # 503 if the work loop has stalled for 2 minutes
        }
        period_seconds = 30
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.grants]

  lifecycle {
    ignore_changes = [template[0].containers[0].image] # CI rolls out new images (deploy.yml)
  }
}

# Webhook senders are unauthenticated at the Google layer; the app verifies HMAC signatures.
resource "google_cloud_run_v2_service_iam_member" "public_api" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Keyless deploys from GitHub Actions (Workload Identity Federation) ---

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}' && assertion.ref == 'refs/heads/main'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  account_id   = "agents-deployer"
  display_name = "GitHub Actions deployer for governed-agents"
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repo}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  repository = google_artifact_registry_repository.images.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_cloud_run_v2_service_iam_member" "deployer_run" {
  for_each = { api = google_cloud_run_v2_service.api.name, worker = google_cloud_run_v2_service.worker.name }
  name     = each.value
  location = var.region
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.deployer.email}"
}

# The deployer may attach only these two runtime identities, nothing else in the project.
resource "google_service_account_iam_member" "deployer_act_as" {
  for_each           = { api = google_service_account.api.name, worker = google_service_account.worker.name }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
