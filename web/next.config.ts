import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Local IPv6 loopback is used by the in-app preview when port 3000 already
  // has an IPv4 listener. It is development-only and does not widen production CORS.
  allowedDevOrigins: ["[::1]"],
};

export default nextConfig;
