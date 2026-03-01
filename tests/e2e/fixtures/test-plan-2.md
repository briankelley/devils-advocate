# Database Migration Plan

## Overview
Migrate user table from single-tenant to multi-tenant schema. Add
`tenant_id` foreign key to all user-facing tables.

## Steps
1. Create `tenants` table with UUID primary key
2. Add `tenant_id` column to `users`, `orders`, `invoices`
3. Backfill existing rows with default tenant
4. Add composite indexes on (tenant_id, id) for all affected tables
5. Update ORM models and queries to filter by tenant
6. Add tenant resolution middleware

## Risks
- Backfill on large tables may lock rows — run during maintenance window
- Existing API consumers need tenant header — breaking change
