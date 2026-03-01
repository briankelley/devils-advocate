# Notification Service Specification

## Purpose
Centralized notification delivery for email, SMS, and push channels.

## Requirements
- Messages are enqueued via internal API (not user-facing)
- At-least-once delivery guarantee
- Retry with exponential backoff (max 5 attempts)
- Template rendering with Jinja2
- Rate limiting: max 100 messages/minute per tenant

## Data Model
- `notifications` table: id, tenant_id, channel, recipient, template_id, payload, status, created_at
- `delivery_attempts` table: id, notification_id, attempt_number, status, error, timestamp

## API
- `POST /internal/notify` — enqueue notification
- `GET /internal/notify/{id}/status` — check delivery status

## Non-Goals
- User preference management (separate service)
- Real-time WebSocket delivery
