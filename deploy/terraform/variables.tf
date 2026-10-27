variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "asia-southeast1"
}

variable "github_repo" {
  description = "owner/name of the repository allowed to deploy, e.g. wshuv-o/governed-agents"
  type        = string
}

variable "image_tag" {
  description = "Image tag to run; CI deploys by commit SHA"
  type        = string
  default     = "bootstrap"
}

variable "db_tier" {
  type    = string
  default = "db-f1-micro"
}

variable "db_high_availability" {
  type    = bool
  default = false
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "max_workers" {
  type    = number
  default = 3
}

variable "model_provider" {
  type    = string
  default = "anthropic"
  validation {
    condition     = contains(["anthropic", "openai"], var.model_provider)
    error_message = "model_provider must be anthropic or openai (any OpenAI-compatible endpoint)."
  }
}

variable "model_name" {
  type    = string
  default = "claude-haiku-5-5"
}
