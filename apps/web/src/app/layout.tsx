import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "./providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "Forge",
  description: "Forge — OSS engineering orchestration platform.",
  icons: {
    icon: [{ url: "/forge-icon.svg", type: "image/svg+xml" }],
  },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background font-sans text-foreground antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
