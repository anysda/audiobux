import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

// `npm run dev` serves the interface and sends /api to `audiobux web` on its default port
export default defineConfig({
  plugins: [svelte()],
  server: { proxy: { '/api': 'http://127.0.0.1:8080' } },
  build: { assetsInlineLimit: 0 },
});
