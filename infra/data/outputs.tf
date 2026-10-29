output "backup_bucket" {
  description = "Name of the S3 bucket for back-ups and the host bundle."
  value       = aws_s3_bucket.backup.bucket
}

output "region" {
  description = "AWS region of the back-up bucket. The scripts use this region."
  value       = var.region
}
