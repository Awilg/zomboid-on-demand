# Built by scripts/deploy.sh.
locals {
  bot_zip = "${path.module}/../../build/bot.zip"
}

resource "aws_lambda_function" "bot" {
  function_name    = "zomboid-bot"
  role             = aws_iam_role.bot.arn
  runtime          = "python3.12"
  architectures    = ["x86_64"]
  handler          = "zomboid_bot.handler.lambda_handler"
  filename         = local.bot_zip
  source_code_hash = filebase64sha256(local.bot_zip)
  timeout          = 10
  # More memory also gives more CPU, so a cold start answers Discord within 3 seconds.
  memory_size = 512

  environment {
    variables = {
      DISCORD_PUBLIC_KEY = var.discord_public_key
      INSTANCE_ID        = aws_instance.zomboid.id
      ZOMBOID_ROLE_ID    = var.zomboid_role_id
      ADMIN_ROLE_ID      = var.admin_role_id
      HARD_LIMIT_MINUTES = "480"
    }
  }
}

# The Discord signature check in the handler is the authentication.
resource "aws_lambda_function_url" "bot" {
  function_name      = aws_lambda_function.bot.function_name
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "function_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.bot.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# Provider 5.x cannot add the InvokedViaFunctionUrl condition. The Discord
# signature check in the handler rejects any unsigned call.
resource "aws_lambda_permission" "invoke_via_url" {
  statement_id  = "AllowPublicInvokeViaUrl"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.bot.function_name
  principal     = "*"
}
