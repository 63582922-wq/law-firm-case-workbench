import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Tauri serves the production UI as bundled static assets. All case writes
  // continue to go through the loopback API; no Next.js server is embedded.
  output: "export",
  images: {
    unoptimized: true,
  },
  // Local IPv6 loopback is used by the in-app preview when port 3000 already
  // has an IPv4 listener. It is development-only and does not widen production CORS.
  allowedDevOrigins: ["[::1]"],
};

export default nextConfig;
