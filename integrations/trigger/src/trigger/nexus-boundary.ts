import { logger, task } from "@trigger.dev/sdk";

/**
 * NEXUS ⇄ Trigger.dev — thin execution boundary (raw witness only).
 *
 * This file owns NO domain logic, NO authority and NO evidence. It accepts an
 * already-decided intent (the caller has minted the idempotency key), performs
 * the background work, and returns a *raw witness* that the NEXUS side must
 * verify and sign. Evidence / lineage are produced in NEXUS, never here.
 */

type BoundaryPayload = {
  /** The decided intent identifier (NEXUS is the authority for this). */
  intent: string;
  /** Opaque work payload; the boundary does not interpret it. */
  data?: Record<string, unknown>;
};

export const nexusBoundaryTask = task({
  id: "nexus-boundary",
  maxDuration: 300,
  run: async (payload: BoundaryPayload, { ctx }) => {
    const startedAt = new Date().toISOString();
    logger.info("nexus-boundary: executing decided intent", {
      intent: payload.intent,
      attempt: ctx.attempt.number,
      environment: ctx.environment.type,
    });

    // Raw witness — the boundary reports *what happened*, not what it means.
    return {
      ok: true,
      witness: {
        intent: payload.intent,
        dataEcho: payload.data ?? {},
        attempt: ctx.attempt.number,
        failedPreviously: ctx.attempt.number > 1,
        ranAt: startedAt,
        environment: ctx.environment.type,
      },
    };
  },
});

/**
 * Proves the retry semantics the boundary relies on: the first attempt fails,
 * the second succeeds. No secret, no network — a deterministic in-process throw.
 */
export const nexusRetryProofTask = task({
  id: "nexus-retry-proof",
  retry: { maxAttempts: 3 },
  run: async (_payload: BoundaryPayload, { ctx }) => {
    if (ctx.attempt.number === 1) {
      throw new Error(`intentional-failure try=${ctx.attempt.number}`);
    }
    return { ok: true, attempt: ctx.attempt.number, failedPreviously: true };
  },
});
