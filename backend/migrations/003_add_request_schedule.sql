ALTER TABLE public.llms_txt_requests
    ADD COLUMN cron_expression TEXT,
    ADD COLUMN status TEXT NOT NULL DEFAULT 'created';
