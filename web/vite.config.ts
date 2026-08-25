/**
 * The app build. `vitest.config.ts` is deliberately a separate file: the rules
 * tests run against the Firestore emulator over the network and need their own
 * timeouts and serial execution, which has nothing to do with bundling a page.
 */

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // shadcn components are copied in with `@/` imports; this is what makes them
  // resolve. Mirrored in tsconfig.json — both are needed, Vite for the bundle
  // and tsc for the type check, and they must agree.
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  // firebase.json's hosting block already points at this. Changing one without
  // the other deploys a stale build.
  build: { outDir: "dist" },
  server: { port: 5173 },
});
