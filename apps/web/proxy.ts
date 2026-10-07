import { type NextRequest, NextResponse } from "next/server";

import { buildCsp, sentryOrigin } from "@/lib/csp";

/**
 * Next.js 16 proxy (formerly middleware): per-request CSP nonce. Next.js reads the nonce
 * from the request's Content-Security-Policy header and applies it to its own scripts.
 */
export function proxy(request: NextRequest): NextResponse {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const sentry = sentryOrigin(process.env.NEXT_PUBLIC_SENTRY_DSN);
  const csp = buildCsp({
    nonce,
    development: process.env.NODE_ENV === "development",
    connectOrigins: sentry ? [sentry] : [],
  });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  return response;
}

export const config = {
  // Pages only: skip API routes (JSON), static assets and prefetches.
  matcher: [
    {
      source: "/((?!api|_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
