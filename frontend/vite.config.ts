import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// One config for both: `defineConfig` from 'vitest/config' is vite's, with the `test`
// block typed. (If a second copy of vite ever appears under node_modules/vitest, its
// Plugin type stops matching this one and the build fails at `tsc -b` - that is the
// symptom to look for, and `npm dedupe` is the fix.)
//
// The dev server proxies /api to the FastAPI backend so the browser sees one origin,
// which is also how the built frontend is served in the single-process deployment.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.REVIEWFLOW_API_URL ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    css: false,
  },
})
