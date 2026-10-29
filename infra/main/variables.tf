variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-2"
}

variable "owner_email" {
  description = "Email address that receives budget alerts."
  type        = string
}

variable "game_password" {
  description = "Password that players type to join the server."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.game_password) > 0 && !can(regex("[\\s\"'`]", var.game_password))
    error_message = "game_password must not be empty and must not contain spaces or quotes."
  }
}

variable "discord_public_key" {
  description = "Public key of the Discord application (hex)."
  type        = string
}

variable "discord_webhook_url" {
  description = "Discord webhook URL for server notices."
  type        = string
  sensitive   = true
}

variable "zomboid_role_id" {
  description = "Discord role ID that may start the server."
  type        = string
}

variable "admin_role_id" {
  description = "Discord role ID that may stop the server."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the game server."
  type        = string
  default     = "m7i-flex.large"
}

variable "disk_gb" {
  description = "Size of the root disk in GB."
  type        = number
  default     = 40
}

variable "backup_bucket" {
  description = "Name of the back-up bucket from the data stack."
  type        = string
}
