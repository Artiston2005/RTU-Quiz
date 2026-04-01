-- SQL Additions for Groq Fallback Limits
-- Run this in your Supabase SQL Editor to add the new limit columns.

ALTER TABLE public.profiles ADD COLUMN IF NOT EXISTS groq_calls_remaining INT DEFAULT 20;
ALTER TABLE public.anonymous_api_usage ADD COLUMN IF NOT EXISTS groq_calls_made INT DEFAULT 0;
