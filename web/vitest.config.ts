import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/**/*.test.ts"],
    // Rules tests talk to the emulator over the network and load a fresh rules
    // set per suite, which is slower than vitest's defaults expect.
    testTimeout: 20_000,
    hookTimeout: 30_000,
    // One suite at a time. Each one clears the emulator between tests, and
    // parallel files would clear each other's data mid-assertion.
    fileParallelism: false,
  },
});
