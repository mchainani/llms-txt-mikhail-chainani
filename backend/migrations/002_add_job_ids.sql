ALTER TABLE public.llms_txt_requests ADD COLUMN job_id TEXT;
ALTER TABLE public.llms_txt_runs ADD COLUMN job_id TEXT;
