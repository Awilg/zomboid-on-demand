terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "zomboid"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  ssm_arn    = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/zomboid"
}

# The RCON password goes on a command line, so it uses letters and digits only.
resource "random_password" "rcon" {
  length  = 24
  special = false
}

resource "aws_ssm_parameter" "game_password" {
  name  = "/zomboid/game_password"
  type  = "SecureString"
  value = var.game_password
}

resource "aws_ssm_parameter" "rcon_password" {
  name  = "/zomboid/rcon_password"
  type  = "SecureString"
  value = random_password.rcon.result
}

resource "aws_ssm_parameter" "discord_webhook_url" {
  name  = "/zomboid/discord_webhook_url"
  type  = "SecureString"
  value = var.discord_webhook_url
}

resource "aws_ssm_parameter" "status" {
  name  = "/zomboid/status"
  type  = "String"
  value = jsonencode({ state = "stopped" })

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "restore_key" {
  name  = "/zomboid/restore_key"
  type  = "String"
  value = "none"

  lifecycle {
    ignore_changes = [value]
  }
}
