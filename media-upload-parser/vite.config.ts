import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

function mediaInfoWasmPath(): Plugin {
  return {
    name: "mediainfo-wasm-path",
    enforce: "pre",
    transform(source, id) {
      const path = id.split("?")[0]?.replace(/\\/g, "/");
      if (!path?.endsWith("/mediainfo.js/dist/esm/MediaInfoModule.js") &&
          !path?.endsWith("/mediainfo.js/dist/esm-bundle/index.js")) return;
      // Fix the library's fallback path for Vite's asset scanner. The parser
      // also supplies an explicit locateFile URL for dev and production.
      return source.replace(
        /new URL\((['"])MediaInfoModule\.wasm\1,\s*import\.meta\.url\)/g,
        "new URL('../MediaInfoModule.wasm', import.meta.url)",
      );
    },
  };
}

export default defineConfig({
  plugins: [mediaInfoWasmPath(), react()],
  // Keep the demo deployable under a subdirectory. WASM is bundled locally.
  base: "./",
  server: { port: 5188, strictPort: true },
  // Prebundle the lazy worker dependency before the first file selection;
  // otherwise Vite reloads the page when it discovers the dependency.
  optimizeDeps: { include: ["mediainfo.js"] },
  worker: { format: "es", plugins: () => [mediaInfoWasmPath()] },
  test: { include: ["src/**/*.test.ts"], environment: "node" },
});
