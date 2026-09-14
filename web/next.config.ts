import type { NextConfig } from "next";
import path from "node:path";

const nextConfig: NextConfig = {
  // The primary product is a self-hosted Web workbench. A standalone server
  // bundle keeps it deployable on a firm server without a Tauri shell.
  output: "standalone",
  // Opt-in for the constrained local acceptance host; production defaults
  // are unchanged. Avoid seven concurrent page-data workers on an 8GB Mac.
  ...(process.env.LAWCASE_BUILD_LOW_MEMORY === "1" ? { experimental: { cpus: 1 } } : {}),
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
  async rewrites() {
    const localApiOrigin = process.env.LAWCASE_LOCAL_API_ORIGIN;
    if (!localApiOrigin) return [];
    return [{
      source: "/api/local/:path*",
      destination: `${localApiOrigin.replace(/\/$/, "")}/api/local/:path*`,
    }];
  },
};

export default nextConfig;
