import { NextResponse, type NextRequest } from "next/server";

import { auth } from "@/auth";
import {
  ALLOWED_METHODS,
  EVENTS_PATH,
  WORKSPACE_COOKIE,
  MAX_BODY_BYTES,
  backendPath,
  clientIp,
  forwardedRequestHeaders,
  isSameOrigin,
  mintBackendToken,
  passthroughResponseHeaders,
  selectedWorkspace,
  visitorSubject,
} from "@/lib/backend-proxy";
import { backendSubjectOf, identityFor, authProviderOf } from "@/lib/identity";
import { log } from "@/lib/log";
import { type ServerEnv, serverEnv, trustedProxyHops } from "@/lib/server-env";

export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ path: string[] }> };

/** Time allowed for the API to *start* an event stream; the stream itself is unbounded. */
const STREAM_CONNECT_TIMEOUT_MS = 10_000;

function problem(status: number, detail: string): NextResponse {
  return NextResponse.json(
    { type: "about:blank", title: "Proxy error", status, detail },
    { status, headers: { "content-type": "application/problem+json", "cache-control": "no-store" } },
  );
}

/** Signed-in users get `github:<id>`; anonymous visitors a pseudonymous `visitor:<hash>`
 *  (only when the client IP is trustworthy - otherwise no token: the API sees our IP). */
async function backendSubject(request: NextRequest, env: ServerEnv): Promise<{ subject: string; login?: string } | null> {
  const session = await auth();
  const identity = session?.user?.provider && session.user.subject ? identityFor(authProviderOf(session.user.provider), session.user.subject) : null;
  if (identity) return { subject: backendSubjectOf(identity), login: session?.user?.login };
  const ip = clientIp(request.headers.get("x-forwarded-for"), trustedProxyHops(env));
  return ip ? { subject: await visitorSubject(ip, env.AUTH_SECRET) } : null;
}

async function proxyStream(url: URL, headers: Headers, request: NextRequest, target: string): Promise<Response> {
  const upstreamAbort = new AbortController();
  const connectTimer = setTimeout(() => upstreamAbort.abort(new DOMException("connect timeout", "TimeoutError")), STREAM_CONNECT_TIMEOUT_MS);
  // Browser went away -> release the API's stream slot immediately.
  request.signal.addEventListener("abort", () => upstreamAbort.abort(), { once: true });
  try {
    const upstream = await fetch(url, {
      headers,
      cache: "no-store",
      redirect: "error",
      signal: upstreamAbort.signal,
    });
    clearTimeout(connectTimer);
    if (!upstream.ok || !upstream.body) {
      const responseHeaders = passthroughResponseHeaders(upstream.headers);
      return new NextResponse(await upstream.text(), { status: upstream.status, headers: responseHeaders });
    }
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-store, no-transform",
        "x-accel-buffering": "no",
        connection: "keep-alive",
      },
    });
  } catch (error) {
    clearTimeout(connectTimer);
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    log("error", "proxy.stream_failure", { target, timed_out: timedOut });
    return problem(timedOut ? 504 : 502, timedOut ? "event stream timed out" : "event stream unavailable");
  }
}

async function proxy(request: NextRequest, context: RouteContext): Promise<Response> {
  if (!ALLOWED_METHODS.has(request.method)) return problem(405, "method not allowed");
  const { path } = await context.params;
  const target = backendPath(path);
  if (!target) return problem(404, "unknown API path");

  const env = serverEnv();
  if (request.method !== "GET") {
    const expectedOrigin = env.AUTH_URL ?? request.nextUrl.origin;
    if (!isSameOrigin(request.headers.get("origin"), expectedOrigin)) {
      log("warn", "proxy.cross_origin_blocked", { method: request.method, target });
      return problem(403, "cross-origin request blocked");
    }
  }

  const headers = forwardedRequestHeaders(request.headers, request.method);
  const caller = await backendSubject(request, env);
  if (caller) {
    const token = await mintBackendToken({
      subject: caller.subject,
      login: caller.login,
      workspaceId: selectedWorkspace(request.cookies.get(WORKSPACE_COOKIE)?.value),
      privateJwk: env.API_JWT_PRIVATE_JWK,
      audience: env.API_JWT_AUDIENCE,
      issuer: env.API_JWT_ISSUER,
    });
    headers.set("authorization", `Bearer ${token}`);
  }

  const url = new URL(target, env.API_BASE_URL);
  url.search = request.nextUrl.search;
  if (request.method === "GET" && target === EVENTS_PATH) return proxyStream(url, headers, request, target);

  let body: string | undefined;
  if (request.method !== "GET") {
    body = await request.text();
    if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) return problem(413, "request body too large");
    if (body) headers.set("content-type", "application/json");
    else body = undefined; // e.g. DELETE without a body
  }

  try {
    const upstream = await fetch(url, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(env.BACKEND_TIMEOUT_MS),
    });
    const payload = upstream.status === 204 ? null : await upstream.text();
    return new NextResponse(payload, { status: upstream.status, headers: passthroughResponseHeaders(upstream.headers) });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    log("error", "proxy.backend_failure", { target, timed_out: timedOut });
    return problem(timedOut ? 504 : 502, timedOut ? "backend timed out" : "backend unavailable");
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
