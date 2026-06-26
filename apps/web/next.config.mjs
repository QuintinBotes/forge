/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Lint only application source during `next build`.
  eslint: { dirs: ["src"] },
  // Surface the API base URL to the typed client at build time when provided.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000",
  },
};

export default nextConfig;
