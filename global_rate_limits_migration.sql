-- Global rate-limit configuration for Gemini/Groq across guest and authenticated users.
-- Edit the single row in public.global_rate_limits (id = 1) from the Supabase dashboard
-- whenever you want to change the app-wide quotas.

ALTER TABLE public.profiles
    ADD COLUMN IF NOT EXISTS groq_calls_remaining INT DEFAULT 20,
    ADD COLUMN IF NOT EXISTS custom_gemini_key TEXT;

ALTER TABLE public.profiles
    ALTER COLUMN last_reset_date SET DEFAULT (timezone('Asia/Kolkata', now()))::date;

ALTER TABLE public.anonymous_api_usage
    ADD COLUMN IF NOT EXISTS groq_calls_made INT DEFAULT 0;

ALTER TABLE public.anonymous_api_usage
    ALTER COLUMN last_reset_date SET DEFAULT (timezone('Asia/Kolkata', now()))::date;

CREATE TABLE IF NOT EXISTS public.global_rate_limits (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    auth_gemini_limit INT NOT NULL DEFAULT 15 CHECK (auth_gemini_limit >= 0),
    auth_groq_limit INT NOT NULL DEFAULT 20 CHECK (auth_groq_limit >= 0),
    guest_gemini_limit INT NOT NULL DEFAULT 3 CHECK (guest_gemini_limit >= 0),
    guest_groq_limit INT NOT NULL DEFAULT 5 CHECK (guest_groq_limit >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.global_rate_limits (
    id,
    auth_gemini_limit,
    auth_groq_limit,
    guest_gemini_limit,
    guest_groq_limit
)
VALUES (1, 15, 20, 3, 5)
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION public.touch_global_rate_limits_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_touch_global_rate_limits_updated_at ON public.global_rate_limits;
CREATE TRIGGER trg_touch_global_rate_limits_updated_at
BEFORE UPDATE ON public.global_rate_limits
FOR EACH ROW
EXECUTE FUNCTION public.touch_global_rate_limits_updated_at();

CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger AS $$
BEGIN
    INSERT INTO public.profiles (
        id,
        full_name,
        api_calls_remaining,
        groq_calls_remaining,
        last_reset_date
    )
    VALUES (
        NEW.id,
        NEW.raw_user_meta_data->>'full_name',
        COALESCE((SELECT auth_gemini_limit FROM public.global_rate_limits WHERE id = 1), 15),
        COALESCE((SELECT auth_groq_limit FROM public.global_rate_limits WHERE id = 1), 20),
        (timezone('Asia/Kolkata', now()))::date
    )
    ON CONFLICT (id) DO NOTHING;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Optional: clamp existing user balances down to the current configured cap.
UPDATE public.profiles
SET
    api_calls_remaining = LEAST(
        COALESCE(api_calls_remaining, (SELECT auth_gemini_limit FROM public.global_rate_limits WHERE id = 1)),
        (SELECT auth_gemini_limit FROM public.global_rate_limits WHERE id = 1)
    ),
    groq_calls_remaining = LEAST(
        COALESCE(groq_calls_remaining, (SELECT auth_groq_limit FROM public.global_rate_limits WHERE id = 1)),
        (SELECT auth_groq_limit FROM public.global_rate_limits WHERE id = 1)
    )
WHERE TRUE;

-- Example updates you can run later in Supabase SQL editor:
-- UPDATE public.global_rate_limits
-- SET auth_gemini_limit = 25, auth_groq_limit = 30, guest_gemini_limit = 5, guest_groq_limit = 8
-- WHERE id = 1;
