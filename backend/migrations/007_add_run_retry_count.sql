ALTER TABLE public.llms_txt_runs ADD COLUMN retry_count INT NOT NULL DEFAULT 0;
