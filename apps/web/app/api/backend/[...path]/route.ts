import { NextResponse, type NextRequest } from "next/server";

import { auth } from "@/auth";
import { ALLOWED_METHODS, MAX_BODY_BYTES, backendPath, mintBackendToken } from "@/lib/backend-proxy";
import { serverEnv } from "@/lib/server-env";

export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ path: string[] }> };

function problem(status: number, detail: string): NextResponse {
  return NextResponse.json(
    { type: "about:blank", title: "Proxy error", status, detail },
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

async function proxy(request: NextRequest, context: RouteContext): Promise<NextResponse> {
  if (!ALLOWED_METHODS.has(request.method)) return problem(405, "method not allowed");
  const { path } = await context.params;
  const target = backendPath(path);
  if (!target) return problem(404, "unknown API path");

  const env = serverEnv();
  const headers = new Headers({ accept: "application/json" });
  const requestId = request.headers.get("x-request-id");
  if (requestId) headers.set("x-request-id", requestId);

  const session = await auth();
  if (session?.user?.login) {
    const token = await mintBackendToken({
      subject: session.user.login,
      role: session.user.role,
      secret: env.API_JWT_SECRET,
      audience: env.API_JWT_AUDIENCE,
      issuer: env.API_JWT_ISSUER,
    });
    headers.set("authorization", `Bearer ${token}`);
  }

  let body: string | undefined;
  if (request.method !== "GET") {
    body = await request.text();
    if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) return problem(413, "request body too large");
    headers.set("content-type", "application/json");
  }

  const url = new URL(target, env.API_BASE_URL);
  url.search = request.nextUrl.search;

  try {
    const upstream = await fetch(url, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(env.BACKEND_TIMEOUT_MS),
    });
    const payload = await upstream.text();
    return new NextResponse(payload, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    console.error("backend proxy failure", { target, timedOut });
    return problem(timedOut ? 504 : 502, timedOut ? "backend timed out" : "backend unavailable");
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
