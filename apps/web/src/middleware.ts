import { NextResponse, type NextRequest } from "next/server";

import { buildContentSecurityPolicy } from "@/lib/security-headers";

/**
 * Per-request Content-Security-Policy with a fresh script nonce.
 *
 * Next.js stamps the nonce from the *request's* `content-security-policy`
 * header onto every script it emits, including the inline document bootstrap.
 * That is why the header is set on both the request (so Next can read it) and
 * the response (so the browser enforces it).
 *
 * Without this the app does not merely lose a hardening layer — it does not
 * run. See {@link buildContentSecurityPolicy}.
 */
function originOf(url: string | undefined): string | null {
  if (!url) {
    return null;
  }
  try {
    return new URL(url).origin;
  } catch {
    // A relative or malformed value contributes no extra origin; 'self' covers
    // the same-origin case, which is the supported deployment.
    return null;
  }
}

export function middleware(request: NextRequest) {
  const nonce = crypto.randomUUID().replaceAll("-", "");
  const isDev = process.env.NODE_ENV !== "production";

  // A dev server reached on its own port talks to the API cross-origin; behind
  // the edge these are same-origin and add nothing.
  const connectSrc = [
    originOf(process.env.NEXT_PUBLIC_API_URL),
    originOf(process.env.NEXT_PUBLIC_WS_URL),
  ].filter((value): value is string => value !== null);

  const csp = buildContentSecurityPolicy({ nonce, isDev, connectSrc });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  // Static assets and images are served with their own caching and carry no
  // inline script, so the per-request nonce would only defeat their caching.
  matcher: [
    {
      source: "/((?!_next/static|_next/image|favicon.ico).*)",
      missing: [
        { type: "header", key: "next-router-prefetch" },
        { type: "header", key: "purpose", value: "prefetch" },
      ],
    },
  ],
};
