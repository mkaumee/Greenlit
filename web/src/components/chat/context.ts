/**
 * Which production the transcript is showing.
 *
 * A tool part cannot be handed a prop: assistant-ui's renderers are named in a
 * config rather than constructed, so there is nowhere to pass one. The project
 * id has to reach the approval card somehow, and a context set once by the
 * screen is the honest way — the alternative is putting it in the tool args,
 * where it would be data the agent appears to have chosen rather than the
 * screen's own state.
 */

import { createContext, useContext } from "react";

export const PendingProject = createContext("");

export const useProjectId = (): string => useContext(PendingProject);
