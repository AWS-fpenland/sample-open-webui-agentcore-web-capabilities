// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          oidc: ['oidc-client-ts', 'react-oidc-context'],
          markdown: ['marked', 'dompurify'],
        },
      },
    },
  },
  server: { port: 5173 },
  preview: { port: 4173 },
});
