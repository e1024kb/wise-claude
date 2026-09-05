// Newline-delimited JSON-RPC 2.0 over a `net.Socket`. One message per line; a chunk may hold a
// partial line or several lines. Server side: `serveConnection(socket, handlers)`. Client side:
// `RpcClient` with per-request timeouts. Notifications flow both ways (the daemon sends `progress`).

import type { Socket } from "node:net";
import {
  RPC_CLIENT_DISCONNECTED,
  RPC_CLIENT_TIMEOUT,
  RPC_DOMAIN_ERROR,
  RPC_INTERNAL_ERROR,
  RPC_INVALID_REQUEST,
  RPC_METHOD_NOT_FOUND,
  RPC_PARSE_ERROR,
} from "./protocol.ts";
import type {
  DomainErrorCode,
  DomainErrorData,
  JsonRpcError,
  JsonRpcId,
  JsonRpcMessage,
  JsonRpcRequest,
  JsonRpcResponse,
} from "./protocol.ts";

// ---- errors ------------------------------------------------------------------------------

/** Thrown by handlers (serialized as the JSON-RPC `error`) and by the client (a failed call). */
export class RpcError extends Error {
  readonly code: number;
  readonly data: unknown;
  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "RpcError";
    this.code = code;
    this.data = data;
  }
  toJSON(): JsonRpcError {
    const err: JsonRpcError = { code: this.code, message: this.message };
    if (this.data !== undefined) err.data = this.data;
    return err;
  }
}

/** A P1 domain error: JSON-RPC code -32000 with `data.code` from the closed list. */
export function domainError(
  code: DomainErrorCode,
  message: string,
  extra: Record<string, unknown> = {},
): RpcError {
  const data: DomainErrorData = { ...extra, code };
  return new RpcError(RPC_DOMAIN_ERROR, message, data);
}

/** `data.code` of a domain error, or `undefined` for any other error. */
export function domainCode(err: unknown): DomainErrorCode | undefined {
  if (!(err instanceof RpcError) || err.code !== RPC_DOMAIN_ERROR) return undefined;
  const data = err.data as { code?: unknown } | undefined;
  return typeof data?.code === "string" ? (data.code as DomainErrorCode) : undefined;
}

// ---- framing --------------------------------------------------------------------------------

/** Buffers chunks and yields complete lines; a trailing partial line waits for the next chunk. */
export class LineFramer {
  private pending = "";
  push(chunk: Buffer | string): string[] {
    this.pending += typeof chunk === "string" ? chunk : chunk.toString("utf8");
    const parts = this.pending.split("\n");
    this.pending = parts.pop() ?? "";
    return parts.map((l) => l.replace(/\r$/, "")).filter((l) => l.trim() !== "");
  }
}

export function encodeMessage(msg: JsonRpcMessage): string {
  return JSON.stringify(msg) + "\n";
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isId(v: unknown): v is JsonRpcId {
  return typeof v === "number" || typeof v === "string";
}

function writeSafe(socket: Socket, msg: JsonRpcMessage): void {
  if (socket.destroyed || !socket.writable) return;
  try {
    socket.write(encodeMessage(msg));
  } catch {
    // The peer went away between the check and the write; nothing to do.
  }
}

// ---- server ----------------------------------------------------------------------------------

export type CallContext = {
  /** Send a notification to this connection (e.g. `progress` while a `wait` blocks). */
  notify: (method: string, params: unknown) => void;
  /** Aborted when the connection closes, so long-running handlers can stop early. */
  signal: AbortSignal;
  connectionId: number;
};

export type RpcHandler = (params: unknown, ctx: CallContext) => unknown | Promise<unknown>;
export type RpcHandlerMap = Record<string, RpcHandler>;

export type ServeOptions = {
  connectionId?: number;
  /** Runs for every request before the handler; throw an `RpcError` to refuse it. */
  guard?: (method: string, ctx: CallContext) => void;
  onClose?: () => void;
  onError?: (err: Error) => void;
};

export type Connection = { close: () => void; readonly signal: AbortSignal };

let nextConnectionId = 1;

function toRpcError(err: unknown): RpcError {
  if (err instanceof RpcError) return err;
  const message = err instanceof Error ? err.message : String(err);
  return new RpcError(RPC_INTERNAL_ERROR, message);
}

/** Dispatch every request on `socket` to `handlers`; requests run concurrently. */
export function serveConnection(
  socket: Socket,
  handlers: RpcHandlerMap,
  opts: ServeOptions = {},
): Connection {
  const framer = new LineFramer();
  const abort = new AbortController();
  const connectionId = opts.connectionId ?? nextConnectionId++;
  const ctx: CallContext = {
    notify: (method, params) => writeSafe(socket, { jsonrpc: "2.0", method, params }),
    signal: abort.signal,
    connectionId,
  };

  const respond = (id: JsonRpcId | null, error: RpcError): void => {
    writeSafe(socket, { jsonrpc: "2.0", id, error: error.toJSON() });
  };

  const dispatch = async (msg: Record<string, unknown>): Promise<void> => {
    const id = isId(msg.id) ? msg.id : null;
    const hasId = "id" in msg && msg.id !== null && msg.id !== undefined;
    if (msg.jsonrpc !== "2.0" || typeof msg.method !== "string") {
      if (hasId || !("method" in msg)) {
        respond(id, new RpcError(RPC_INVALID_REQUEST, "invalid request"));
      }
      return;
    }
    const method = msg.method;
    const handler = handlers[method];
    let result: unknown;
    try {
      opts.guard?.(method, ctx);
      if (!handler) throw new RpcError(RPC_METHOD_NOT_FOUND, `method not found: ${method}`);
      result = await handler(msg.params, ctx);
    } catch (err) {
      if (hasId) respond(id, toRpcError(err));
      return;
    }
    if (hasId) writeSafe(socket, { jsonrpc: "2.0", id: id as JsonRpcId, result: result ?? null });
  };

  socket.on("data", (chunk: Buffer) => {
    for (const line of framer.push(chunk)) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(line);
      } catch {
        respond(null, new RpcError(RPC_PARSE_ERROR, "parse error"));
        continue;
      }
      if (!isObject(parsed)) {
        respond(null, new RpcError(RPC_INVALID_REQUEST, "invalid request"));
        continue;
      }
      void dispatch(parsed);
    }
  });
  socket.on("error", (err: Error) => opts.onError?.(err));
  socket.on("close", () => {
    abort.abort();
    opts.onClose?.();
  });

  return {
    close: () => socket.destroy(),
    signal: abort.signal,
  };
}

// ---- client -------------------------------------------------------------------------------------

export type Notification = { method: string; params: unknown };
export type NotificationListener = (n: Notification) => void;

export type CallOptions = { timeoutMs?: number };

type Pending = {
  resolve: (v: unknown) => void;
  reject: (e: RpcError) => void;
  timer: NodeJS.Timeout | undefined;
};

export class RpcClient {
  readonly socket: Socket;
  private readonly framer = new LineFramer();
  private readonly pending = new Map<JsonRpcId, Pending>();
  private readonly listeners = new Set<NotificationListener>();
  private nextId = 1;
  private closed = false;
  /** Default per-call timeout; `0` disables. */
  defaultTimeoutMs: number;

  constructor(socket: Socket, opts: CallOptions = {}) {
    this.socket = socket;
    this.defaultTimeoutMs = opts.timeoutMs ?? 30_000;
    socket.on("data", (chunk: Buffer) => this.onData(chunk));
    socket.on("error", () => {
      // The close event that follows rejects every pending call.
    });
    socket.on("close", () => this.onClose());
  }

  get isClosed(): boolean {
    return this.closed;
  }

  /** Subscribe to server notifications; returns the unsubscribe function. */
  onNotification(listener: NotificationListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notify(method: string, params?: unknown): void {
    writeSafe(this.socket, { jsonrpc: "2.0", method, params });
  }

  call(method: string, params?: unknown, opts: CallOptions = {}): Promise<unknown> {
    if (this.closed) {
      return Promise.reject(new RpcError(RPC_CLIENT_DISCONNECTED, "connection closed"));
    }
    const id = this.nextId++;
    const timeoutMs = opts.timeoutMs ?? this.defaultTimeoutMs;
    return new Promise<unknown>((resolve, reject) => {
      const timer =
        timeoutMs > 0
          ? setTimeout(() => {
              this.pending.delete(id);
              reject(new RpcError(RPC_CLIENT_TIMEOUT, `${method}: no reply in ${timeoutMs} ms`));
            }, timeoutMs)
          : undefined;
      this.pending.set(id, { resolve, reject, timer });
      const req: JsonRpcRequest = { jsonrpc: "2.0", id, method, params };
      writeSafe(this.socket, req);
    });
  }

  close(): void {
    if (this.closed) return;
    this.socket.destroy();
    this.onClose();
  }

  private onData(chunk: Buffer): void {
    for (const line of this.framer.push(chunk)) {
      let msg: unknown;
      try {
        msg = JSON.parse(line);
      } catch {
        continue;
      }
      if (!isObject(msg)) continue;
      if (typeof msg.method === "string") {
        const n: Notification = { method: msg.method, params: msg.params };
        for (const l of this.listeners) l(n);
        continue;
      }
      const res = msg as unknown as JsonRpcResponse;
      if (res.id === null || res.id === undefined) continue;
      const p = this.pending.get(res.id);
      if (!p) continue;
      this.pending.delete(res.id);
      if (p.timer) clearTimeout(p.timer);
      if ("error" in res && res.error) {
        p.reject(new RpcError(res.error.code, res.error.message, res.error.data));
      } else {
        p.resolve((res as { result?: unknown }).result);
      }
    }
  }

  private onClose(): void {
    if (this.closed) return;
    this.closed = true;
    for (const [id, p] of this.pending) {
      this.pending.delete(id);
      if (p.timer) clearTimeout(p.timer);
      p.reject(new RpcError(RPC_CLIENT_DISCONNECTED, "connection closed"));
    }
  }
}
