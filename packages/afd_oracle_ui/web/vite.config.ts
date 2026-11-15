import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built into the Python package so `uv run afd-oracle ui` serves it; `npm run dev` proxies the API for development.
export default defineConfig({
  plugins: [react()],
  build: { outDir: '../afd_oracle_ui/static', emptyOutDir: true },
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8765' } },
})
