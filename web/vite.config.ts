/**
 * The app build. `vitest.config.ts` is deliberately a separate file: the rules
 * tests run against the Firestore emulator over the network and need their own
 * timeouts and serial execution, which has nothing to do with bundling a page.
 */

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // firebase.json's hosting block already points at this. Changing one without
  // the other deploys a stale build.
  build: { outDir: "dist" },
  server: { port: 5173 },
});
