/** Next.js config — proxies /api/* to the FastAPI signal engine (server-side, so the
 *  browser only ever talks to this one origin and never to localhost). */
const API_ORIGIN = process.env.SIGNAL_API_ORIGIN || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // Sandboxed preview hosts (e.g. https://3000-<id>.e2b.app) must be allowed to load dev assets.
  allowedDevOrigins: [
    "localhost",
    "127.0.0.1",
    "*.e2b.app",
    "*.e2b.dev",
    "*.arena.ai",
    "*.usercontent.goog",
  ],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_ORIGIN}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
        ],
      },
    ];
  },
};

export default nextConfig;
