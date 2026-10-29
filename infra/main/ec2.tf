data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_security_group" "zomboid" {
  name_prefix = "zomboid-"
  description = "Project Zomboid game traffic. No SSH; use SSM Session Manager."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Game traffic"
    from_port   = 16261
    to_port     = 16262
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "zomboid" {
  ami                                  = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type                        = var.instance_type
  iam_instance_profile                 = aws_iam_instance_profile.zomboid.name
  vpc_security_group_ids               = [aws_security_group.zomboid.id]
  instance_initiated_shutdown_behavior = "stop"

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    bucket = var.backup_bucket
    region = var.region
  })

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.disk_gb
    delete_on_termination = true
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
    # Docker is not used, so one hop is enough.
    http_put_response_hop_limit = 1
  }

  tags = {
    Name = "zomboid"
  }

  # A deploy must never replace the instance.
  lifecycle {
    ignore_changes = [ami, user_data]
  }
}
