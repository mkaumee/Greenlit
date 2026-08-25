/**
 * One Firebase app, built once at module load.
 *
 * `VITE_USE_EMULATOR=1` points everything at the local emulators instead of
 * the real project, which is how this is developed: `make e2e` fills the
 * emulator with a project, four items and twelve negotiations, so the panel
 * has something on it from the first run — and the loop can be run again in
 * another terminal to watch rows move.
 *
 * Against the emulator the config values are ignored, so the committed
 * placeholders are fine there. Against the real project they are not, and the
 * failure that produces deep inside the SDK is unreadable — hence the check.
 */

import { initializeApp, type FirebaseApp } from "firebase/app";
import {
  connectAuthEmulator,
  getAuth,
  GoogleAuthProvider,
  signInWithPopup,
  signOut,
  type Auth,
  type User,
} from "firebase/auth";
import {
  connectFirestoreEmulator,
  getFirestore,
  type Firestore,
} from "firebase/firestore";

import {
  EMULATOR_PROJECT,
  firebaseConfig,
  isPlaceholder,
} from "./firebase-config";

export const USE_EMULATOR = import.meta.env["VITE_USE_EMULATOR"] === "1";

if (!USE_EMULATOR && isPlaceholder()) {
  throw new Error(
    "src/firebase-config.ts still holds placeholders. Run " +
      "`firebase apps:sdkconfig web --project <project-id>` and paste the " +
      "result in, or develop against the emulator with VITE_USE_EMULATOR=1.",
  );
}

const app: FirebaseApp = initializeApp(
  USE_EMULATOR ? { ...firebaseConfig, projectId: EMULATOR_PROJECT } : firebaseConfig,
);

export const auth: Auth = getAuth(app);
export const db: Firestore = getFirestore(app);

if (USE_EMULATOR) {
  connectAuthEmulator(auth, "http://127.0.0.1:9099", { disableWarnings: true });
  connectFirestoreEmulator(db, "127.0.0.1", 8080);
}

/**
 * Google sign-in, not anonymous.
 *
 * Reads are gated behind `isSignedIn()`, so some identity is required either
 * way. It is Google because Phase 6's Approve screen needs the `producer`
 * custom claim that `scripts/grant_producer.py` sets on a real account, and an
 * anonymous identity cannot carry one. Doing it now is twenty lines that are
 * not redone later.
 */
export const signIn = async (): Promise<void> => {
  await signInWithPopup(auth, new GoogleAuthProvider());
};

export const signOutOfEverything = async (): Promise<void> => {
  await signOut(auth);
};

export type { User };
