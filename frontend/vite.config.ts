import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'BACKEND_');
  const proxy = {
    '/llms-txt': {
      target: env.BACKEND_URL || 'http://127.0.0.1:8000',
      changeOrigin: true,
    },
  };
  return {
    plugins: [react()],
    server: { proxy },
    preview: { proxy },
    test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'] },
  };
});
