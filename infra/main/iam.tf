data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "zomboid-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "instance_ssm_core" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "instance" {
  statement {
    sid       = "BackupObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.backup_bucket}/*"]
  }

  statement {
    sid       = "BackupList"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.backup_bucket}"]
  }

  statement {
    sid       = "ReadParameters"
    actions   = ["ssm:GetParameter"]
    resources = ["${local.ssm_arn}/*"]
  }

  statement {
    sid     = "WriteParameters"
    actions = ["ssm:PutParameter"]
    resources = [
      aws_ssm_parameter.status.arn,
      aws_ssm_parameter.restore_key.arn,
    ]
  }
}

resource "aws_iam_role_policy" "instance" {
  name   = "zomboid-instance"
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.instance.json
}

resource "aws_iam_instance_profile" "zomboid" {
  name = "zomboid-instance"
  role = aws_iam_role.instance.name
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "bot" {
  name               = "zomboid-bot"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "bot_logs" {
  role       = aws_iam_role.bot.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "bot" {
  statement {
    sid       = "StartInstance"
    actions   = ["ec2:StartInstances"]
    resources = [aws_instance.zomboid.arn]
  }

  statement {
    sid       = "DescribeInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadStatus"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.status.arn]
  }

  statement {
    sid     = "SendStopCommand"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.zomboid.arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }
}

resource "aws_iam_role_policy" "bot" {
  name   = "zomboid-bot"
  role   = aws_iam_role.bot.id
  policy = data.aws_iam_policy_document.bot.json
}
