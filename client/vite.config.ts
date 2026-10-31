import path from "path";
import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    preserveSymlinks: true,
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "out",
    rollupOptions: {
      /* SEULE `index.html` est construite. `banc/banc.html` existe donc
       * uniquement pour le serveur de développement et ne part JAMAIS en
       * production — c'est ce qui distingue ce banc de l'ancienne route
       * `#/bench`, dont le code se retrouvait mesurablement dans le bundle
       * servi. Voir CONTEXTE_MIGRATION_FLUID.md §7. */
      input: path.resolve(__dirname, "index.html"),
    },
  },
});
