/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // HARD-07: emit a self-contained production server (.next/standalone) so the
  // runtime container ships only the traced server + static assets, not the
  // whole pnpm workspace (smaller image, smaller attack surface / SBOM).
  output: "standalone",
  // Note: Next.js 16 removed the built-in ESLint integration (`next lint` and the
  // `eslint` config key). Linting now runs via `pnpm lint` (eslint.config.mjs).
  // Surface the API base URL to the typed client at build time when provided.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
