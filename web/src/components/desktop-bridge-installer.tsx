"use client";

import { useEffect } from "react";
import { installDesktopBridge } from "@/lib/desktop-bridge";

export function DesktopBridgeInstaller() {
  useEffect(() => {
    installDesktopBridge();

    return () => {
      if (typeof window !== "undefined" && window.__TAURI_INTERNALS__ !== undefined) {
        delete window.lawCaseDesktop;
      }
    };
  }, []);

  return null;
}
