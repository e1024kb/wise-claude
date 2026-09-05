// `approval` and `ask` steps (gates, D13): build the Gate the harness renders, and turn the
// client's `answer` value into the step's terminal state.

import { RPC_INVALID_PARAMS } from "../protocol.ts";
import { RpcError } from "../rpc.ts";
import type { ApprovalStep, AskStep, Gate } from "../types.ts";
import { headline } from "./agent.ts";

export type GateStep = ApprovalStep | AskStep;

export const APPROVAL_OPTIONS: readonly { value: string; label: string }[] = [
  { value: "approve", label: "Approve" },
  { value: "reject", label: "Reject" },
];

export function isGateStep(step: { type: string }): step is GateStep {
  return step.type === "approval" || step.type === "ask";
}

/** Gate for a rendered step. Approval always offers approve / reject; ask mirrors its options. */
export function buildGate(step: GateStep, gateId: string): Gate {
  const gate: Gate = {
    gate_id: gateId,
    step: step.id,
    kind: step.type,
    message: step.message.trim(),
  };
  if (step.type === "approval") {
    gate.options = APPROVAL_OPTIONS.map((o) => ({ ...o }));
    return gate;
  }
  if (step.options && step.options.length > 0) {
    gate.options = step.options.map((o) => ({ value: o, label: o }));
  }
  if (step.allow_text !== undefined) gate.allow_text = step.allow_text;
  return gate;
}

export type GateDecision = {
  status: "completed" | "failed";
  verdict: string;
  /** `ask` only: the run output to record. */
  output?: { name: string; value: string };
};

function asText(value: string | string[]): string {
  return Array.isArray(value) ? value.join(", ") : value;
}

/** Validate and apply an answer. Throws `RpcError(INVALID_PARAMS)` on a value the gate refuses. */
export function decideGate(step: GateStep, value: string | string[]): GateDecision {
  const text = asText(value).trim();
  if (step.type === "approval") {
    if (text === "approve") return { status: "completed", verdict: "approved" };
    if (text === "reject") return { status: "failed", verdict: "rejected" };
    throw new RpcError(
      RPC_INVALID_PARAMS,
      `answer: approval ${step.id} takes "approve" or "reject", got ${JSON.stringify(text)}`,
    );
  }
  const options = step.options ?? [];
  const allowText = step.allow_text ?? options.length === 0;
  if (text === "" || (!allowText && !options.includes(text))) {
    throw new RpcError(
      RPC_INVALID_PARAMS,
      `answer: ask ${step.id} takes one of ${JSON.stringify(options)}, got ${JSON.stringify(text)}`,
    );
  }
  const name = step.output ?? step.id;
  return {
    status: "completed",
    verdict: headline(`${name}=${text}`),
    output: { name, value: text },
  };
}
