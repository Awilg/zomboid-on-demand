output "instance_id" {
  description = "ID of the game server instance."
  value       = aws_instance.zomboid.id
}

output "interactions_url" {
  description = "Set this URL as the Interactions Endpoint URL in the Discord developer portal."
  value       = aws_lambda_function_url.bot.function_url
}
