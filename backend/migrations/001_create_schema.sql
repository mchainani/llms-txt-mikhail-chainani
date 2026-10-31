CREATE TABLE public.llms_txt_requests (
    id UUID PRIMARY KEY,
    url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE public.llms_txt_runs (
    id UUID PRIMARY KEY,
    request_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL,
    file_id UUID
);

CREATE TABLE public.llms_txt_files (
    id UUID PRIMARY KEY,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PostgreSQL creates this index in public, the same schema as its table.
CREATE INDEX ak_llms_txt_runs_request_id
    ON public.llms_txt_runs (request_id, created_at DESC);
