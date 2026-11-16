import type { Metadata } from "next";
import { headers } from "next/headers";
import type { ReactNode } from "react";

import { THEME_INIT_SCRIPT } from "@/lib/theme";

import { Providers } from "./providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Swarm Code Factory",
  description: "Swarm Code Factory — From specification to shipped software.",
};

export default async function RootLayout({
  children,
}: {
  children: ReactNode;
}) {
  // The middleware's per-request CSP nonce; without it the theme script is blocked.
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script
          nonce={nonce}
          suppressHydrationWarning
          dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }}
        />
      </head>
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
