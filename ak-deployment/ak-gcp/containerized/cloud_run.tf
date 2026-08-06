# Service account for Cloud Run — like an IAM role in AWS
resource "google_service_account" "run_sa" {
  project      = var.project_id
  account_id   = local.sa_id
  display_name = "Cloud Run SA for ${var.product_alias}-${var.env_alias}"
}

# Let the service write logs to Cloud Logging
resource "google_project_iam_member" "run_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.run_sa.email}"
}

# Let the service read/write Firestore if enabled
resource "google_project_iam_member" "run_firestore" {
  count   = var.create_firestore_database ? 1 : 0
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.run_sa.email}"
}

resource "null_resource" "network_route_cleanup" {
  triggers = {
    project_id   = var.project_id
    network_name = local.network_name != null ? local.network_name : ""
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      # Delete auto-generated default routes that can block VPC deletion
      if [ -n "${self.triggers.network_name}" ]; then
        for rt in $(gcloud compute routes list \
          --project=${self.triggers.project_id} \
          --filter="network.name=${self.triggers.network_name} AND nextHopGateway~default-internet-gateway" \
          --format="value(name)" 2>/dev/null); do
          echo "Deleting auto-generated route: $rt"
          gcloud compute routes delete "$rt" \
            --project=${self.triggers.project_id} --quiet 2>/dev/null || true
        done
      fi
    EOT
  }
}

resource "null_resource" "egress_address_cleanup" {
  triggers = {
    project_id = var.project_id
    region     = var.region
    subnetwork = element(reverse(split("/", local.private_subnet_id)), 0)
  }

  provisioner "local-exec" {
    when    = destroy
    command = <<-EOT
      for i in $(seq 1 18); do
        addrs=$(gcloud compute addresses list \
          --project=${self.triggers.project_id} \
          --regions=${self.triggers.region} \
          --filter="name~^serverless-ipv4 AND subnetwork~/${self.triggers.subnetwork}$" \
          --format="value(name)" 2>/dev/null)
        [ -z "$addrs" ] && break
        for a in $addrs; do
          echo "Deleting lingering Direct VPC egress address: $a"
          gcloud compute addresses delete "$a" \
            --project=${self.triggers.project_id} \
            --region=${self.triggers.region} --quiet 2>/dev/null || true
        done
        sleep 10
      done
    EOT
  }
}


# The Cloud Run service — this replaces ECS Fargate + ALB + Target Group
# Cloud Run handles all of that in one resource
resource "google_cloud_run_v2_service" "service" {
  name                = local.service_name
  project             = var.project_id
  location            = var.region
  deletion_protection = false

  launch_stage = "GA"

  template {
    # Service account — who the container runs as
    service_account = google_service_account.run_sa.email

    timeout = "${var.timeout}s"

    # Scaling — like ECS desired_count + autoscaling
    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    # VPC access — so the container can reach Redis, Firestore, etc.
    vpc_access {
      network_interfaces {
        network    = local.network_id
        subnetwork = local.private_subnet_id
      }
      egress = "PRIVATE_RANGES_ONLY"
    }

    # The container itself — like the ECS task definition
    containers {
      name  = local.prefix
      image = module.docker_image.image_url

      # Port the container listens on
      ports {
        container_port = var.container_port
      }

      # Resource limits — like ECS cpu and memory
      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
      }

      # Health check — like the ALB target group health check
      startup_probe {
        http_get {
          path = var.health_check_endpoint
          port = var.container_port
        }
        initial_delay_seconds = 5
        period_seconds        = 10
        failure_threshold     = 3
      }

      liveness_probe {
        http_get {
          path = var.health_check_endpoint
          port = var.container_port
        }
        period_seconds = 30
      }

      # Environment variables — merge user vars with Redis/Firestore config
      dynamic "env" {
        for_each = merge(
          var.environment_variables,
          {
            API_BASE_PATH  = var.api_base_path
            API_VERSION    = var.api_version
            AGENT_ENDPOINT = var.agent_endpoint
          },
          local.redis_url != null ? {
            AK_SESSION__REDIS__URL = local.redis_url
          } : {},
          local.firestore_db_name != null ? {
            AK_SESSION__TYPE                       = "firestore"
            AK_SESSION__FIRESTORE__COLLECTION_NAME = module.firestore[0].collection_name
            AK_SESSION__FIRESTORE__PROJECT_ID      = var.project_id
            AK_SESSION__FIRESTORE__DATABASE_ID     = module.firestore[0].database_name
          } : {}
        )
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  labels = var.tags

  depends_on = [
    google_project_iam_member.run_logging,
    module.docker_image,
    null_resource.egress_address_cleanup
  ]
}

# Configure log retention on the project's default Cloud Logging bucket.
# GCP logs to Cloud Logging automatically — this sets how long logs are kept.
# Equivalent of aws_cloudwatch_log_group retention_in_days in AWS.
resource "google_logging_project_bucket_config" "default_logs" {
  count          = var.log_retention_days != null ? 1 : 0
  project        = var.project_id
  location       = "global"
  retention_days = var.log_retention_days
  bucket_id      = "_Default"
}

# Allow unauthenticated direct invocation of the Cloud Run service URL.
# When allow_unauthenticated_invocation = true (default), the Cloud Run URL is publicly
# accessible. Authentication is enforced at the API Gateway level (JWT authorizer).
# Set allow_unauthenticated_invocation = false for stricter network-level isolation —
# only the API Gateway service agent will be granted roles/run.invoker.
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count    = var.allow_unauthenticated_invocation ? 1 : 0
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# Look up the project number to construct the API Gateway service agent email.
data "google_project" "project" {
  project_id = var.project_id
}

# When allow_unauthenticated_invocation = false, grant the API Gateway service agent
# roles/run.invoker so the gateway can still reach Cloud Run.
# Without this, flipping allow_unauthenticated_invocation = false would block all traffic.
resource "google_cloud_run_v2_service_iam_member" "gateway_invoker" {
  count    = var.allow_unauthenticated_invocation ? 0 : 1
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.project.number}@gcp-sa-apigateway.iam.gserviceaccount.com"
}
