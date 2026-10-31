import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from './api';

interface Resource<T> {
  key: string | null;
  data: T | null;
  error: string | null;
  errorStatus: number | null;
  loading: boolean;
}

export function useResource<T>(path: string | null, pollWhile?: (data: T) => boolean) {
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<Resource<T>>({ key: path, data: null, error: null, errorStatus: null, loading: !!path });
  const reload = useCallback(() => setVersion((value) => value + 1), []);

  useEffect(() => {
    if (!path) { return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    setState((old) => ({ key: path, data: old.key === path ? old.data : null, error: null, errorStatus: null, loading: true }));
    async function load() {
      try {
        const data = await api<T>(path!, { signal: controller.signal });
        if (controller.signal.aborted) { return; }
        setState({ key: path, data, error: null, errorStatus: null, loading: false });
        if (pollWhile?.(data)) { timer = setTimeout(load, 5000); }
      } catch (error) {
        if (!controller.signal.aborted) {
          setState((old) => ({ ...old, loading: false, error: error instanceof Error ? error.message : 'Something went wrong.', errorStatus: error instanceof ApiError ? error.status : null }));
        }
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [path, version, pollWhile]);

  const current = state.key === path && path ? state : { data: null, error: null, errorStatus: null, loading: !!path };
  return { ...current, reload };
}
