import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // Tauri serves the production UI as bundled static assets. All case writes
  // continue to go through the loopback API; no Next.js server is embedded.
  output: "export",
  images: {
    unoptimized: true,
  },
  // The official case registry is deliberately kept once at the repository
  // root so the backend validator and the bundled desktop UI cannot drift.
  turbopack: {
    root: path.join(__dirname, ".."),
  },
  // Local IPv6 loopback is used by the in-app preview when port 3000 already
  // has an IPv4 listener. It is development-only and does not widen production CORS.
  allowedDevOrigins: ["[::1]"],
};

export default nextConfig;
